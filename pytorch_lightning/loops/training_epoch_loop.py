# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any, Dict, Iterator, List, Optional, Union

import torch

import pytorch_lightning as pl
from pytorch_lightning.loops.base import Loop
from pytorch_lightning.loops.training_batch_loop import TrainingBatchLoop
from pytorch_lightning.trainer.connectors.logger_connector.result import ResultCollection
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.model_helpers import is_overridden
from pytorch_lightning.utilities.signature_utils import is_param_in_hook_signature
from pytorch_lightning.utilities.types import STEP_OUTPUT
from pytorch_lightning.utilities.warnings import WarningCache


class TrainingEpochLoop(Loop):
    """ Runs over all batches in a dataloader (one epoch). """

    def __init__(self, min_steps: int, max_steps: int):
        super().__init__()
        self.min_steps: int = min_steps
        self.max_steps: int = max_steps

        self.global_step: int = 0

        # the total batch index across all epochs
        self.total_batch_idx: int = 0
        # the current batch index in the loop that runs over the dataloader(s)
        self.iteration_count: int = 0
        # the current split index when the batch gets split into chunks in truncated backprop through time
        self.split_idx: Optional[int] = None

        self._dataloader_idx: Optional[int] = None
        self._should_stop: bool = False

        self.is_last_batch: Optional[bool] = None
        self.batches_seen: int = 0
        self.warning_cache: WarningCache = WarningCache()
        self.epoch_output: Optional[List[List[STEP_OUTPUT]]] = None

        self.batch_loop: Optional[TrainingBatchLoop] = None

        self._results = ResultCollection(training=True)

    @property
    def results(self) -> ResultCollection:
        return self._results

    @property
    def batch_idx(self) -> int:
        """Returns the current batch index (within this epoch)"""
        return self.iteration_count

    @property
    def done(self) -> bool:
        """Returns whether the training should be stopped.
        The criteria are that the number of steps reached the max steps,
        the last batch is reached or the trainer signals to stop (e.g. by early stopping).
        """
        max_steps_reached = self.max_steps is not None and self.global_step >= self.max_steps
        return max_steps_reached or self.trainer.should_stop or self._num_training_batches_reached(self.is_last_batch)

    def connect(self, trainer: 'pl.Trainer', *args: Any, **kwargs: Any) -> None:
        """Connects the loop with all necessary parts like trainer and accelerators"""

        # TODO(@justusschock): should we forward *args and **kwargs to lower loops?
        # TODO(@justusschock): can we make the trainer a proxy here?
        self.trainer = trainer
        self.batch_loop = TrainingBatchLoop()
        self.batch_loop.connect(trainer)

    def reset(self) -> None:
        """Resets the internal state of the loop for a new run"""
        self.iteration_count = 0
        self.batches_seen = 0
        self.is_last_batch = False
        self._dataloader_idx = 0
        self._should_stop = False

        # track epoch output
        self.epoch_output = [[] for _ in range(self.batch_loop.num_active_optimizers(self.total_batch_idx))]

    def on_run_start(self, *args: Any, **kwargs: Any) -> None:
        # hook
        self.trainer.logger_connector.on_epoch_start()
        self.trainer.call_hook("on_epoch_start")
        self.trainer.call_hook("on_train_epoch_start")

    def advance(self, dataloader_iter: Iterator, **kwargs: Any) -> None:
        """Runs a single training batch.

        Args:
            dataloader_iter: the iterator over the dataloader producing the new batch

        Raises:
            StopIteration: When the epoch is canceled by the user returning -1
        """
        _, (batch, is_last) = next(dataloader_iter)
        self.is_last_batch = is_last

        # ------------------------------------
        # TRAINING_STEP + TRAINING_STEP_END
        # ------------------------------------
        with self.trainer.profiler.profile("run_training_batch"):
            batch_output = self.batch_loop.run(batch, self.iteration_count, self._dataloader_idx)
            self.batches_seen += 1

        # when returning -1 from train_step, we end epoch early
        if batch_output.signal == -1:
            raise StopIteration

        # update non-plateau LR schedulers
        # update epoch-interval ones only when we are at the end of training epoch
        self.update_lr_schedulers('step', update_plateau_schedulers=False)
        if self._num_training_batches_reached(is_last):
            self.update_lr_schedulers('epoch', update_plateau_schedulers=False)

        batch_end_outputs = [opt_idx_out for opt_idx_out in batch_output.training_step_output if len(opt_idx_out)]
        processed_batch_end_outputs = self._prepare_outputs(batch_end_outputs, batch_mode=True)

        # hook
        self.trainer.call_hook(
            'on_train_batch_end', processed_batch_end_outputs, batch, self.iteration_count, self._dataloader_idx
        )
        self.trainer.call_hook('on_batch_end')
        self.trainer.logger_connector.on_batch_end()

        # figure out what to track for epoch end
        self.track_epoch_end_reduce_metrics(self.epoch_output, batch_end_outputs)

        # -----------------------------------------
        # SAVE METRICS TO LOGGERS AND PROGRESS_BAR
        # -----------------------------------------
        self.trainer.logger_connector.update_train_step_metrics()

    def on_advance_end(self):
        """Runs validation and Checkpointing if necessary.

        Raises:
            StopIteration: if :attr:`done` evaluates to ``True`` to finish this epoch
        """
        # -----------------------------------------
        # VALIDATE IF NEEDED + CHECKPOINT CALLBACK
        # -----------------------------------------
        should_check_val = self.should_check_val_fx(self.iteration_count, self.is_last_batch)
        if should_check_val:
            self.trainer.validating = True
            self._run_validation()
            self.trainer.training = True

        # -----------------------------------------
        # SAVE LOGGERS (ie: Tensorboard, etc...)
        # -----------------------------------------
        self.save_loggers_on_train_batch_end()

        # update plateau LR scheduler after metrics are logged
        self.update_lr_schedulers('step', update_plateau_schedulers=True)
        self.trainer.checkpoint_connector.has_trained = True

        self.total_batch_idx += 1

        # progress global step according to grads progress
        self.increment_accumulated_grad_global_step()

        if self.done:
            raise StopIteration

    def _run_validation(self):
        # reload dataloaders
        self.trainer.fit_loop.validation_loop.reload_evaluation_dataloaders()

        with torch.no_grad():
            self.trainer.fit_loop.validation_loop.run()

    def on_run_end(self) -> List[List[STEP_OUTPUT]]:
        """Calls the on_epoch_end hook.

        Returns:
            The output of each training step for each optimizer

        Raises:
            MisconfigurationException: ``train_epoch_end`` does not return ``None``
        """
        if self.batches_seen == 0:
            # dataloader/iterator did not produce a batch
            return

        # inform logger the batch loop has finished
        self.trainer.logger_connector.epoch_end_reached()

        # prepare epoch output
        processed_outputs = self._prepare_outputs(self.epoch_output, batch_mode=False)

        # get the model and call model.training_epoch_end
        model = self.trainer.lightning_module

        if is_overridden('training_epoch_end', model):
            # run training_epoch_end
            # refresh the result for custom logging at the epoch level
            model._current_fx_name = 'training_epoch_end'

            # lightningmodule hook
            training_epoch_end_output = model.training_epoch_end(processed_outputs)

            if training_epoch_end_output is not None:
                raise MisconfigurationException(
                    'training_epoch_end expects a return of None. '
                    'HINT: remove the return statement in training_epoch_end'
                )

        # call train epoch end hooks
        self._on_train_epoch_end_hook(processed_outputs)
        self.trainer.call_hook('on_epoch_end')
        self.trainer.logger_connector.on_epoch_end()
        return self.epoch_output

    def teardown(self) -> None:
        """Frees memory of tracked epoch outputs."""
        self.epoch_output = None

    def _on_train_epoch_end_hook(self, processed_epoch_output: List[List[STEP_OUTPUT]]) -> None:
        """Runs ``on_train_epoch_end hook``."""
        # We cannot rely on Trainer.call_hook because the signatures might be different across
        # lightning module and callback
        # As a result, we need to inspect if the module accepts `outputs` in `on_train_epoch_end`

        # This implementation is copied from Trainer.call_hook
        hook_name = "on_train_epoch_end"
        prev_fx_name = self.trainer.lightning_module._current_fx_name
        self.trainer.lightning_module._current_fx_name = hook_name

        # always profile hooks
        with self.trainer.profiler.profile(hook_name):

            # first call trainer hook
            if hasattr(self.trainer, hook_name):
                trainer_hook = getattr(self.trainer, hook_name)
                trainer_hook(processed_epoch_output)

            # next call hook in lightningModule
            model_ref = self.trainer.lightning_module
            if is_overridden(hook_name, model_ref):
                hook_fx = getattr(model_ref, hook_name)
                if is_param_in_hook_signature(hook_fx, "outputs"):
                    self.warning_cache.deprecation(
                        "The signature of `ModelHooks.on_train_epoch_end` has changed in v1.3."
                        " `outputs` parameter has been deprecated."
                        " Support for the old signature will be removed in v1.5",
                    )
                    model_ref.on_train_epoch_end(processed_epoch_output)
                else:
                    model_ref.on_train_epoch_end()

            # call the accelerator hook
            if hasattr(self.trainer.accelerator, hook_name):
                accelerator_hook = getattr(self.trainer.accelerator, hook_name)
                accelerator_hook()

        # restore current_fx when nested context
        self.trainer.lightning_module._current_fx_name = prev_fx_name

    def _num_training_batches_reached(self, is_last_batch: bool = False) -> bool:
        """Checks if we are in the last batch or if there are more batches to follow."""

        # TODO: Can we combine this with training_batch_loop's arg that does a similar check?
        return self.batches_seen == self.trainer.num_training_batches or is_last_batch

    def track_epoch_end_reduce_metrics(
        self, epoch_output: List[List[STEP_OUTPUT]], batch_end_outputs: STEP_OUTPUT
    ) -> None:
        """Adds the batch outputs to the epoch outputs and prepares reduction"""
        hook_overridden = self._should_add_batch_output_to_epoch_output()
        if not hook_overridden:
            return

        # track the outputs to reduce at the end of the epoch
        for opt_idx, opt_outputs in enumerate(batch_end_outputs):
            # with 1 step (no tbptt) don't use a sequence at epoch end
            if (
                isinstance(opt_outputs, list) and len(opt_outputs) == 1
                and not isinstance(opt_outputs[0], ResultCollection)
            ):
                opt_outputs = opt_outputs[0]

            epoch_output[opt_idx].append(opt_outputs)

    def _should_add_batch_output_to_epoch_output(self) -> bool:
        """
        We add to the epoch outputs if
        1. The model defines training_epoch_end OR
        2. The model overrides on_train_epoch_end which has `outputs` in the signature
        """
        # TODO: in v1.5 this only needs to check if training_epoch_end is overridden
        lightning_module = self.trainer.lightning_module
        if is_overridden("training_epoch_end", lightning_module):
            return True

        if is_overridden("on_train_epoch_end", lightning_module):
            model_hook_fx = getattr(lightning_module, "on_train_epoch_end")
            if is_param_in_hook_signature(model_hook_fx, "outputs"):
                return True

        return False

    @staticmethod
    def _prepare_outputs(
        outputs: List[List[List['ResultCollection']]],
        batch_mode: bool,
    ) -> Union[List[List[List[Dict]]], List[List[Dict]], List[Dict], Dict]:
        """
        Extract required information from batch or epoch end results.

        Args:
            outputs: A 3-dimensional list of ``ResultCollection`` objects with dimensions:
                ``[optimizer outs][batch outs][tbptt steps]``.

            batch_mode: If True, ignore the batch output dimension.

        Returns:
            The cleaned outputs with ``ResultCollection`` objects converted to dictionaries.
            All list dimensions of size one will be collapsed.
        """
        processed_outputs = []
        for opt_outputs in outputs:
            # handle an edge case where an optimizer output is the empty list
            if len(opt_outputs) == 0:
                continue

            processed_batch_outputs = []

            if batch_mode:
                opt_outputs = [opt_outputs]

            for batch_outputs in opt_outputs:
                processed_tbptt_outputs = []

                if isinstance(batch_outputs, ResultCollection):
                    batch_outputs = [batch_outputs]

                for tbptt_output in batch_outputs:
                    out = tbptt_output.extra
                    if tbptt_output.minimize is not None:
                        out['loss'] = tbptt_output.minimize.detach()
                    processed_tbptt_outputs.append(out)

                # if there was only one tbptt step then we can collapse that dimension
                if len(processed_tbptt_outputs) == 1:
                    processed_tbptt_outputs = processed_tbptt_outputs[0]
                processed_batch_outputs.append(processed_tbptt_outputs)

            # batch_outputs should be just one dict (or a list of dicts if using tbptt) per optimizer
            if batch_mode:
                processed_batch_outputs = processed_batch_outputs[0]
            processed_outputs.append(processed_batch_outputs)

        # if there is only one optimiser then we collapse that dimension
        if len(processed_outputs) == 1:
            processed_outputs = processed_outputs[0]
        return processed_outputs

    def update_lr_schedulers(self, interval: str, update_plateau_schedulers: bool) -> None:
        """updates the lr schedulers based on the given interval"""
        if interval == "step" and self.batch_loop.should_accumulate():
            return
        self.trainer.optimizer_connector.update_learning_rates(
            interval=interval,
            update_plateau_schedulers=update_plateau_schedulers,
            opt_indices=[opt_idx for opt_idx, _ in self.batch_loop.get_active_optimizers(self.total_batch_idx)],
        )

    def increment_accumulated_grad_global_step(self) -> None:
        """increments global step"""
        num_accumulated_batches_reached = self.batch_loop._accumulated_batches_reached()
        num_training_batches_reached = self._num_training_batches_reached()

        # progress global step according to grads progress
        if num_accumulated_batches_reached or num_training_batches_reached:
            self.global_step = self.trainer.accelerator.update_global_step(
                self.total_batch_idx, self.trainer.global_step
            )

    def should_check_val_fx(self, batch_idx: int, is_last_batch: bool) -> bool:
        """ Decide if we should run validation. """
        if not self.trainer.enable_validation:
            return False

        is_val_check_epoch = (self.trainer.current_epoch + 1) % self.trainer.check_val_every_n_epoch == 0
        if not is_val_check_epoch:
            return False

        # val_check_batch is inf for iterable datasets with no length defined
        is_infinite_dataset = self.trainer.val_check_batch == float('inf')
        if is_last_batch and is_infinite_dataset:
            return True

        if self.trainer.should_stop:
            return True

        # TODO(@awaelchli): let training/eval loop handle logic around limit_*_batches and val_check_batch
        is_val_check_batch = is_last_batch
        if isinstance(self.trainer.limit_train_batches, int) and is_infinite_dataset:
            is_val_check_batch = (batch_idx + 1) % self.trainer.limit_train_batches == 0
        elif self.trainer.val_check_batch != float('inf'):
            is_val_check_batch = (batch_idx + 1) % self.trainer.val_check_batch == 0
        return is_val_check_batch

    def save_loggers_on_train_batch_end(self) -> None:
        """Flushes loggers to disk"""
        # when loggers should save to disk
        should_flush_logs = self.trainer.logger_connector.should_flush_logs
        if should_flush_logs and self.trainer.is_global_zero and self.trainer.logger is not None:
            self.trainer.logger.save()
