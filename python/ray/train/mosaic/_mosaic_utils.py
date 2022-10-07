import math
from typing import Any, Dict, Tuple, Iterable, Optional, List
import torch
import os
from ray.air import session
from ray.data.dataset import Dataset
from ray.train.mosaic.mosaic_checkpoint import MosaicCheckpoint
from ray.tune.syncer import _DefaultSyncer

from composer.loggers import Logger, InMemoryLogger
from composer.loggers.logger_destination import LoggerDestination
from composer.core.state import State
from composer.callbacks.checkpoint_saver import CheckpointSaver
from composer.core.callback import Callback


class _mosaic_iterator:
    """An iterator that provides batches of given size from Ray Dataset.

    Each item returned by the iterator is a list of pandas DataFrame column.
    The labels for the columns to be included should be provided by the user
    as part of `trainer_init_config`, and the columns will be in the same
    order as the list of labels.
    """

    def __init__(self, dataset, batch_size, labels):
        self.dataset = dataset
        self.labels = labels
        self.batch_iter = self.dataset.iter_torch_batches(batch_size=batch_size)

    def __next__(self):
        next_data = next(self.batch_iter)
        return [next_data[label] for label in self.labels]


class _ray_dataset_mosaic_iterable:
    """A wrapper that provides an iterator over Ray Dataset for training Composer models.

    Composer trainer can take an Iterable as a dataloader, so we provide an Iterable
    wrappervover Ray Dataset for Composer models' data consumption. Each item provided
    by the iterator should be the next batch to be trained on. The `__iter__` function
    returns `_mosaic_iterator`, which iterates through batches of size provided by the
    user as part of `trainer_init_config`. There is no default batch_size, and it must
    be provided for MosaicTrainer to run. The length of the Iterable is the number of
    batches contained in the given dataset.

    The dataset should be of pandas DataFrame type, and the labels for the columns to be
    included in the batch should be provided as part of the `trainer_init_config`.

    Args:
        dataset: Ray Dataset that will be iteratred over
        batch_size: the size of each batch that will be returned by the iterator
        labels: the labels of the dataset columns to be included in each batch
    """

    def __init__(self, dataset, batch_size, labels):
        self.dataset = dataset
        self.batch_size = batch_size
        self.labels = labels
        self.total_samples = dataset.count()

    def __len__(self):
        return math.ceil(self.total_samples / self.batch_size)

    def __iter__(self):
        return _mosaic_iterator(self.dataset, self.batch_size, self.labels)


def process_datasets(
    train_dataset: Dataset, eval_dataset: Dataset, batch_size, labels
) -> Tuple["Iterable", "Iterable"]:
    """Convert Ray train and validation to Iterables."""
    if train_dataset:
        train_torch_iterable = _ray_dataset_mosaic_iterable(
            train_dataset, batch_size, labels
        )
    else:
        train_torch_iterable = None

    if eval_dataset:
        eval_torch_iterable = _ray_dataset_mosaic_iterable(
            eval_dataset, batch_size, labels
        )
    else:
        eval_torch_iterable = None

    return train_torch_iterable, eval_torch_iterable


def get_load_path_if_exists(checkpoint, load_path, remote_dir, load_from_remote):
    _load_path = None
    cwd = os.getcwd()
    if checkpoint:
        load_dir = None
        checkpoint_dict = checkpoint.to_dict()
        if load_from_remote:
            if checkpoint_dict["remote_dir"] is None:
                raise KeyError(
                    "the checkpoint to resume from does not have a \
                    remote directory"
                )
            syncer = _DefaultSyncer()
            syncer.sync_down(checkpoint_dict["remote_dir"], cwd)
            syncer.wait_or_retry()
            load_dir = cwd
        else:
            load_dir = checkpoint_dict["working_directory"]
        checkpoint_file = (
            load_path if load_path else checkpoint_dict["all_checkpoints"][-1]
        )
        _load_path = os.path.join(load_dir, checkpoint_file)
    elif load_from_remote:
        assert (
            remote_dir is not None
        ), "Loading from remote, but `remote_dir` is not \
            provided in `trainer_init_config`"
        assert load_path is not None, "Loading from saved data but no path is given"
        syncer = _DefaultSyncer()
        syncer.sync_down(remote_dir, cwd)
        _load_path = os.path.join(cwd, load_path)
        syncer.wait_or_retry()
    elif load_path:
        _load_path = load_path
    return _load_path


class RayLogger(LoggerDestination):
    """A logger to relay information logged by composer models to ray.

    This logger allows utilizing all necessary logging and logged data handling provided
    by the Composer library. All the logged information is saved in the data dictionary
    every time a new information is logged, but to reduce unnecessary reporting, the
    most up-to-date logged information is reported as metrics every batch checkpoint and
    epoch checkpoint (see Composer's Event module for more details).

    Because ray's metric dataframe will not include new keys that is reported after the
    very first report call, any logged information with the keys not included in the
    first batch checkpoint would not be retrievable after training. In other words, if
    the log level is greater than `LogLevel.BATCH` for some data, they would not be
    present in `Result.metrics_dataframe`. To allow preserving those information, the
    user can provide keys to be always included in the reported data by using `keys`
    argument in the constructor. For `MosaicTrainer`, use
    `trainer_init_config['log_keys']` to populate these keys.

    Note that in the Event callback functions, we remove unused variables, as this is
    practiced in Mosaic's composer library.

    Args:
        keys: the key values that will be included in the reported metrics.
    """

    def __init__(self, keys: List[str] = None) -> None:
        self.data = {}
        if keys:
            for key in keys:
                self.data[key] = None

    def log_metrics(self, metrics: Dict[str, Any], step: Optional[int] = None) -> None:
        self.data.update(metrics.items())
        for key, val in self.data.items():
            if isinstance(val, torch.Tensor):
                self.data[key] = val.item()

    def batch_checkpoint(self, state: State, logger: Logger) -> None:
        del logger  # unused
        session.report(self.data)

    def epoch_checkpoint(self, state: State, logger: Logger) -> None:
        del logger  # unused
        session.report(self.data)


class RayTrainReportCallback(Callback):
    """A callback to report the saved checkpoints at the very end of training.

    Upon the ``close`` event in ``composer.trainer.Trainer.fit``, a ``MosaicCheckpoint``
    object is created and reported. This checkpoint contains the working directory,
    list of ``InMemoryLogger`` objects of the trainer, list of relative paths to all
    composer checkpoint files, and the URI to the remote directory (if there is one).
    If a remote directory is provided, then the checkpointed files are uploaded before
    the checkpoint is reproted. Note that the synced files or the paths would be
    different from those that will result when using ``SyncConfig`` in ``RunConfig``.
    Only the files related to the composer library's training logic would be saved; this
    is to provide as identical a view as when training pure composer models.

    Example:
        .. code-block:: python
            # create a MosaicTrainer
            mosaic_trainer =  MosaicTrainer(
                    trainer_init_per_worker=trainer_init_per_worker,
                    trainer_init_config=trainer_init_config,
                    scaling_config=scaling_config
                )
            result = mosaic_trainer.fit()

            chkpt_dict = result.checkpoint.to_dict()

            in_memory_logger = chkpt_dict["in_memory_logger"]
            last_checkpoint = chkpt_dict["remote_dir"]
            all_checkpoints = chkpt_dict["all_checkpoints"]
            working_directory = chkpt_dict["working_directory"]

    Args:
        in_memory_logger: A list of Composer InMemoryLogger that would be used in
            Composer trainer initialization.
        ray_logger : A ``RayLogger`` object that is created to relay the logged data via
            Ray. The last updated metrics will be reported with the checkpoint at the
            end of the trianing.
        checkpoint_saver: A Composer ``CheckpointSaver`` that the callback will wrap.
            If this argument is provided, then the parent class is initialized with the
            passed in ``CheckpointSaver`` object's attributes. Otherwise, the parent
            class is initialized with args provided.
        remote_dir: A URI to a remote storage.
    """

    def __init__(
        self,
        in_memory_logger: List[InMemoryLogger],
        ray_logger: RayLogger,
        checkpoint_savers: List[CheckpointSaver],
        remote_dir: str = None,
    ):
        self.in_memory_logger = in_memory_logger
        self.ray_logger = ray_logger
        self.checkpoint_savers = checkpoint_savers
        self.remote_dir = remote_dir

    def close(self, state: State, logger: Logger) -> None:
        del logger  # unused
        all_checkpoints = []
        for checkpoint_saver in self.checkpoint_savers:
            all_checkpoints.extend(checkpoint_saver.saved_checkpoints)

        checkpoint = MosaicCheckpoint.from_dict(
            {
                "working_directory": os.getcwd(),
                "in_memory_logger": self.in_memory_logger,
                "all_checkpoints": all_checkpoints,
                "remote_dir": self.remote_dir,
            }
        )

        if self.remote_dir:
            syncer = _DefaultSyncer()
            syncer.sync_up(os.getcwd(), self.remote_dir)
            syncer.wait_or_retry()

        session.report(metrics=self.ray_logger.data, checkpoint=checkpoint)
