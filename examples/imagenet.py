import hydra
import torch
from torch.nn import functional as F
from torchvision.models import resnet50

from homura import optim, lr_scheduler, enable_accimage, get_num_nodes, init_distributed, reporters
from homura.trainers import SupervisedTrainer
from homura.vision.data import DATASET_REGISTRY

is_distributed = False


def _handle_argparse():
    # handle raises Error with non key=val format
    # which causes problem with torch.distributed.launch
    import sys
    import re

    original_argv = sys.argv
    hydra_pattern = re.compile(r'[^-|^=]+=[^=]+')
    hydra_argv = [k for k in original_argv if re.match(hydra_pattern, k) is not None]
    non_hydra_argv = [k for k in original_argv[1:] if k not in hydra_argv]
    if any([not k.startswith('-') for k in non_hydra_argv]):
        # non_hydra_argv should start with -
        raise RuntimeError(f"Wrong argument is given: check one of {non_hydra_argv}")
    help_argv = [k for k in original_argv if k == '-h' or k == '--help']
    sys.argv = [original_argv[0]] + hydra_argv + help_argv
    if any(['local_rank' in k for k in non_hydra_argv]):
        global is_distributed
        is_distributed = True


@hydra.main("config/imagenet.yaml")
def main(cfg):
    if is_distributed:
        init_distributed(use_horovod=cfg.distributed.use_horovod,
                         backend=cfg.distributed.backend,
                         init_method=cfg.distributed.init_method)
    if cfg.enable_accimage:
        enable_accimage()

    model = resnet50()
    optimizer = optim.SGD(lr=1e-1 * cfg.batch_size * get_num_nodes() / 256, momentum=0.9, weight_decay=1e-4)
    scheduler = lr_scheduler.MultiStepLR([30, 60, 80])
    train_loader, test_loader = DATASET_REGISTRY("fast_imagenet" if cfg.use_fast_collate else
                                                 "imagenet")(cfg.batch_size,
                                                             train_size=cfg.batch_size * 50 if cfg.debug else None,
                                                             test_size=cfg.batch_size * 50 if cfg.debug else None,
                                                             num_workers=cfg.num_workers,
                                                             use_prefetcher=cfg.use_prefetcher)

    use_multi_gpus = not is_distributed and torch.cuda.device_count() > 1
    with SupervisedTrainer(model,
                           optimizer,
                           F.cross_entropy,
                           reporters=[reporters.TensorboardReporter(".")],
                           scheduler=scheduler,
                           data_parallel=use_multi_gpus,
                           use_amp=cfg.use_amp,
                           use_cuda_nonblocking=True,
                           use_sync_bn=cfg.use_sync_bn,
                           use_horovod=cfg.distributed.use_horovod,
                           report_accuracy_topk=5) as trainer:

        for epoch in trainer.epoch_range(cfg.epochs):
            trainer.train(train_loader)
            trainer.test(test_loader)

        print(f"Max Test Accuracy={max(trainer.reporter.history('accuracy/test')):.3f}")


if __name__ == '__main__':
    import warnings

    # to suppress annoying warnings from PIL
    warnings.filterwarnings("ignore", "(Possibly )?corrupt EXIF data", UserWarning)
    import sys

    _handle_argparse()
    main()
