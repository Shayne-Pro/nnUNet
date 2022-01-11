import sys
from datetime import datetime
from time import time, sleep
from typing import Union, Optional, Tuple, List

import numpy as np
import pytorch_lightning as pl
import torch
from batchgenerators.transforms.abstract_transforms import AbstractTransform, Compose
from batchgenerators.transforms.color_transforms import BrightnessMultiplicativeTransform, \
    ContrastAugmentationTransform, GammaTransform
from batchgenerators.transforms.noise_transforms import GaussianNoiseTransform, GaussianBlurTransform
from batchgenerators.transforms.resample_transforms import SimulateLowResolutionTransform
from batchgenerators.transforms.spatial_transforms import SpatialTransform, MirrorTransform
from batchgenerators.transforms.utility_transforms import RemoveLabelTransform, RenameTransform, NumpyToTensor
from batchgenerators.utilities.file_and_folder_operations import join, load_json, isfile, save_json, maybe_mkdir_p
from pytorch_lightning.utilities.types import STEP_OUTPUT, EPOCH_OUTPUT
from sklearn.model_selection import KFold

from nnunetv2.paths import nnUNet_preprocessed, nnUNet_results
from nnunetv2.training.data_augmentation.compute_initial_patch_size import get_patch_size
from nnunetv2.training.data_augmentation.custom_transforms.cascade_transforms import MoveSegAsOneHotToData, \
    ApplyRandomBinaryOperatorTransform, RemoveRandomConnectedComponentFromOneHotEncodingTransform
from nnunetv2.training.data_augmentation.custom_transforms.deep_supervision_donwsampling import \
    DownsampleSegForDSTransform2
from nnunetv2.training.data_augmentation.custom_transforms.limited_length_multithreaded_augmenter import \
    LimitedLenWrapper
from nnunetv2.training.data_augmentation.custom_transforms.masking import MaskTransform
from nnunetv2.training.data_augmentation.custom_transforms.region_based_training import \
    ConvertSegmentationToRegionsTransform
from nnunetv2.training.data_augmentation.custom_transforms.transforms_for_dummy_2d import Convert2DTo3DTransform, \
    Convert3DTo2DTransform
from nnunetv2.training.dataloading.data_loader_2d import nnUNetDataLoader2D
from nnunetv2.training.dataloading.data_loader_3d import nnUNetDataLoader3D
from nnunetv2.training.dataloading.nnunet_dataset import nnUNetDataset
from nnunetv2.training.dataloading.utils import unpack_dataset, get_case_identifiers
from nnunetv2.training.lr_scheduler.polylr import PolyLRScheduler
from nnunetv2.utilities.dataset_name_id_conversion import maybe_convert_to_dataset_name
from nnunetv2.utilities.default_n_proc_DA import get_allowed_n_proc_DA
from nnunetv2.utilities.get_network_from_plans import get_network_from_plans
import torch.nn.functional as F

from nnunetv2.utilities.tensor_utilities import sum_tensor


class nnUNetModule(pl.LightningModule):
    def __init__(self, dataset_name_or_id: Union[int, str], plans_name: str, configuration: str, fold: int,
                 unpack_dataset: bool = True, folder_with_segs_from_previous_stage: str = None):
        """
        This trainer should work with single and multi GPU training. Important! If you want to train multi-node
        multi-GPU then the data should be in the same physical location! (can have different paths, but must be the
        same folder for all nodes). This has to do with unpacking.

        We do not use lightnings logging functionality because there is too much magic going on under the hood. We
        need something simple.
        """
        super().__init__()

        self.dataset_name = maybe_convert_to_dataset_name(dataset_name_or_id)

        self.preprocessed_dataset_folder_base = join(nnUNet_preprocessed, self.dataset_name)
        self.plans_file = join(self.preprocessed_dataset_folder_base, plans_name + 'json')
        self.plans = load_json(self.plans_file)
        self.preprocessed_dataset_folder = join(self.preprocessed_dataset_folder_base,
                                                self.plans['configurations'][self.configuration]["data_identifier"])
        self.dataset_json = load_json(join(self.preprocessed_dataset_folder_base, 'dataset.json'))

        # labels can either be a list of int (regular training) or a list of tuples of int (region-based training)
        self.labels, self.regions = self._handle_labels()

        self.configuration = configuration
        self.unpack_dataset = unpack_dataset
        self.folder_with_segs_from_previous_stage = folder_with_segs_from_previous_stage
        self.fold = fold

        # if you want to swap out the network architecture you need to change that here. You do not need to use the
        # plans at all if you don't want to, just make sure your architecture is compatible with the patch size
        # dictated by the plans!
        # We made this a
        self.network = get_network_from_plans(self.plans, configuration)

        self.initial_lr = 1e-2
        self.weight_decay = 3e-5
        self.oversample_foreground_percent = 0.33

        self.num_iterations_per_epoch = 250
        self.num_val_iterations_per_epoch = 50

        self.output_folder = join(nnUNet_results, dataset_name_or_id,
                                  self.__class__.__name__ + '__' + plans_name + "__" + configuration)
        maybe_mkdir_p(self.output_folder)
        self.log_file = None

    def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:

    def _handle_labels(self) -> Tuple[List, Union[List, None]]:
        # first we need to check if we have to run region-based training
        region_needed = any([isinstance(i, tuple) and len(i) > 1 for i in self.dataset_json['labels'].values()])
        if region_needed:
            assert 'regions_class_order' in self.dataset_json.keys(), 'if region-based training is requested via ' \
                                                                      'dataset.json then you need to define ' \
                                                                      'regions_class_order as well, ' \
                                                                      'see documentation!'  # TODO add this
            regions = list(self.dataset_json['labels'].values())
            assert len(self.dataset_json['regions_class_order']) == len(regions), 'regions_class_order must have ans ' \
                                                                                  'many entries as there are ' \
                                                                                  'regions'
            # I can't think of a smarter way to do this
            all_labels = []
            for r in regions:
                if isinstance(r, tuple):
                    for ri in r:
                        all_labels.append(ri)
                else:
                    all_labels.append(r)
        else:
            all_labels = list(self.dataset_json['labels'].values())
            all_labels.sort()
            regions = None
        return all_labels, regions

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.network.parameters(), self.initial_lr, weight_decay=self.weight_decay,
                                    momentum=0.99, nesterov=True)
        lr_scheduler = PolyLRScheduler(optimizer, self.initial_lr, self.ep)
        return optimizer, lr_scheduler

    def on_train_epoch_start(self) -> None:
        self.lr_schedulers().step(self.current_epoch)

    def do_split(self):
        """
        The default split is a 5 fold CV on all available training cases. nnU-Net will create a split (it is seeded,
        so always the same) and save it as splits_final.pkl file in the preprocessed data directory.
        Sometimes you may want to create your own split for various reasons. For this you will need to create your own
        splits_final.pkl file. If this file is present, nnU-Net is going to use it and whatever splits are defined in
        it. You can create as many splits in this file as you want. Note that if you define only 4 splits (fold 0-3)
        and then set fold=4 when training (that would be the fifth split), nnU-Net will print a warning and proceed to
        use a random 80:20 data split.
        :return:
        """

        if self.fold == "all":
            # if fold==all then we use all images for training and validation
            case_identifiers = get_case_identifiers(self.preprocessed_dataset_folder)
            tr_keys = case_identifiers
            val_keys = []
        else:
            splits_file = join(self.dataset_directory, "splits_final.json")

            # if the split file does not exist we need to create it
            if not isfile(splits_file):
                self.print_to_log_file("Creating new 5-fold cross-validation split...")
                splits = []
                all_keys_sorted = np.sort(list(self.dataset.keys()))
                kfold = KFold(n_splits=5, shuffle=True, random_state=12345)
                for i, (train_idx, test_idx) in enumerate(kfold.split(all_keys_sorted)):
                    train_keys = np.array(all_keys_sorted)[train_idx]
                    test_keys = np.array(all_keys_sorted)[test_idx]
                    splits.append({})
                    splits[-1]['train'] = train_keys
                    splits[-1]['val'] = test_keys
                save_json(splits, splits_file)

            else:
                self.print_to_log_file("Using splits from existing split file:", splits_file)
                splits = load_json(splits_file)
                self.print_to_log_file("The split file contains %d splits." % len(splits))

            self.print_to_log_file("Desired fold for training: %d" % self.fold)
            if self.fold < len(splits):
                tr_keys = splits[self.fold]['train']
                val_keys = splits[self.fold]['val']
                self.print_to_log_file("This split has %d training and %d validation cases."
                                       % (len(tr_keys), len(val_keys)))
            else:
                self.print_to_log_file("INFO: You requested fold %d for training but splits "
                                       "contain only %d folds. I am now creating a "
                                       "random (but seeded) 80:20 split!" % (self.fold, len(splits)))
                # if we request a fold that is not in the split file, create a random 80:20 split
                rnd = np.random.RandomState(seed=12345 + self.fold)
                keys = np.sort(list(self.dataset.keys()))
                idx_tr = rnd.choice(len(keys), int(len(keys) * 0.8), replace=False)
                idx_val = [i for i in range(len(keys)) if i not in idx_tr]
                tr_keys = [keys[i] for i in idx_tr]
                val_keys = [keys[i] for i in idx_val]
                self.print_to_log_file("This random 80:20 split has %d training and %d validation cases."
                                       % (len(tr_keys), len(val_keys)))
        if any([i in val_keys for i in tr_keys]):
            self.print_to_log_file('WARNING: Some validation cases are also in the training set. Please check the '
                                   'splits.json or ignore if this is intentional.')
        return tr_keys, val_keys

    def print_to_log_file(self, *args, also_print_to_console=True, add_timestamp=True):
        if self.trainer.is_global_zero:
            timestamp = time()
            dt_object = datetime.fromtimestamp(timestamp)

            if add_timestamp:
                args = ("%s:" % dt_object, *args)

            if self.log_file is None:
                maybe_mkdir_p(self.output_folder)
                timestamp = datetime.now()
                self.log_file = join(self.output_folder, "training_log_%d_%d_%d_%02.0d_%02.0d_%02.0d.txt" %
                                     (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute,
                                      timestamp.second))
                with open(self.log_file, 'w') as f:
                    f.write("Starting... \n")
            successful = False
            max_attempts = 5
            ctr = 0
            while not successful and ctr < max_attempts:
                try:
                    with open(self.log_file, 'a+') as f:
                        for a in args:
                            f.write(str(a))
                            f.write(" ")
                        f.write("\n")
                    successful = True
                except IOError:
                    print("%s: failed to log: " % datetime.fromtimestamp(timestamp), sys.exc_info())
                    sleep(0.5)
                    ctr += 1
            if also_print_to_console:
                print(*args)

    def get_tr_and_val_datasets(self):
        # create dataset split
        tr_keys, val_keys = self.do_split()

        # load the datasets for training and validation. Note that we always draw random samples so we really don't
        # care about distributing training cases across GPUs.
        dataset_tr = nnUNetDataset(self.preprocessed_dataset_folder, tr_keys,
                                   folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage)
        dataset_val = nnUNetDataset(self.preprocessed_dataset_folder, val_keys,
                                    folder_with_segs_from_previous_stage=self.folder_with_segs_from_previous_stage)
        return dataset_tr, dataset_val

    def configure_rotation_dummyDA_mirroring_and_inital_patch_size(self):
        """
        This function is stupid and vertainly one of the weakest spots of this implementation. Not entirely sure how we can fix it.
        """
        patch_size = self.plans['configurations'][self.configuration]["patch_size"]
        dim = len(patch_size)
        # todo rotation should be defined dynamically based on patch size (more isotropic patch sizes = more rotation)
        if dim == 2:
            do_dummy_2d_data_aug = False
            # todo revisit this parametrization
            if max(self.patch_size) / min(self.patch_size) > 1.5:
                rotation_for_DA = {
                    'x': (-15. / 360 * 2. * np.pi, 15. / 360 * 2. * np.pi),
                    'y': (0, 0),
                    'z': (0, 0)
                }
            else:
                rotation_for_DA = {
                    'x': (-180. / 360 * 2. * np.pi, 180. / 360 * 2. * np.pi),
                    'y': (0, 0),
                    'z': (0, 0)
                }
            mirror_axes = (0, 1)
        elif dim == 3:
            # todo this is not ideal. We could also have patch_size (64, 16, 128) in which case a full 180deg 2d rot would be bad
            # order of the axes is determined by spacing, not image size
            do_dummy_2d_data_aug = (max(patch_size) / patch_size[0]) > self.anisotropy_threshold
            if do_dummy_2d_data_aug:
                # why do we rotate 180 deg here all the time? We should also restrict it
                rotation_for_DA = {
                    'x': (-180. / 360 * 2. * np.pi, 180. / 360 * 2. * np.pi),
                    'y': (0, 0),
                    'z': (0, 0)
                }
            else:
                rotation_for_DA = {
                    'x': (-30. / 360 * 2. * np.pi, 30. / 360 * 2. * np.pi),
                    'y': (-30. / 360 * 2. * np.pi, 30. / 360 * 2. * np.pi),
                    'z': (-30. / 360 * 2. * np.pi, 30. / 360 * 2. * np.pi),
                }
            mirror_axes = (0, 1, 2)
        else:
            raise RuntimeError()

        # todo this function is stupid. It doesn't even use the correct scale range (we keep things as they were in the
        #  old nnunet for now)
        initial_patch_size = get_patch_size(patch_size[-dim:],
                                            *rotation_for_DA.values(),
                                            (0.85, 1.25))
        if do_dummy_2d_data_aug:
            initial_patch_size = [patch_size[0]] + initial_patch_size

        return rotation_for_DA, do_dummy_2d_data_aug, initial_patch_size, mirror_axes

    def get_dataloaders(self):
        # we use the patch size to determine whether we need 2D or 3D dataloaders. We also use it to determine whether
        # we need to use dummy 2D augmentation (in case of 3D training) and what our initial patch size should be
        patch_size = self.plans['configurations'][self.configuration]["patch_size"]
        dim = len(patch_size)

        # needed for deep supervision: how much do we need to downscale the segmentation targets for the different
        # outputs?
        deep_supervision_scales = [[1, 1, 1]] + list(list(i) for i in 1 / np.cumprod(np.vstack(
            self.plans['configurations'][self.configuration]['pool_op_kernel_sizes']), axis=0))[:-1]

        rotation_for_DA, do_dummy_2d_data_aug, initial_patch_size, mirror_axes = \
            self.configure_rotation_dummyDA_mirroring_and_inital_patch_size()

        # training pipeline
        tr_transforms = self.get_training_transforms(
            patch_size, rotation_for_DA, deep_supervision_scales, mirror_axes, do_dummy_2d_data_aug,
            order_resampling_data=1, order_resampling_seg=0,
            use_mask_for_norm=self.plans['configurations'][self.configuration]['use_mask_for_norm'],
            is_cascaded=False, all_labels=self.labels)

        # validation pipeline
        val_transforms = self.get_validation_transforms()

        dl_tr, dl_val = self.get_plain_dataloaders(initial_patch_size, dim)

        allowed_num_processes = get_allowed_n_proc_DA()
        mt_gen_train = LimitedLenWrapper(self.num_iterations_per_epoch, dl_tr, tr_transforms,
                                         allowed_num_processes, 1, None, True, 0.02)
        mt_gen_val = LimitedLenWrapper(self.num_val_iterations_per_epoch, dl_val, val_transforms,
                                       max(1, allowed_num_processes // 2), 1, None, True, 0.02)

        # maybe unpack
        if self.unpack_dataset and self.global_rank == 0:
            unpack_dataset(self.preprocessed_dataset_folder, unpack_segmentation=True, overwrite_existing=True,
                           num_processes=max(1, get_allowed_n_proc_DA() // 2))

        # get the bois going
        mt_gen_train._start()
        mt_gen_val._start()

        return dl_tr, dl_val

    def get_plain_dataloaders(self, initial_patch_size: Tuple[int, ...], dim: int):
        dataset_tr, dataset_val = self.get_tr_and_val_datasets()
        if dim == 2:
            dl_tr = nnUNetDataLoader2D(dataset_tr, self.plans['configurations'][self.configuration]['batch_size'],
                                       initial_patch_size,
                                       self.plans['configurations'][self.configuration]['patch_size'],
                                       oversample_foreground_percent=self.oversample_foreground_percent,
                                       sampling_probabilities=None, pad_sides=None)
            dl_val = nnUNetDataLoader2D(dataset_val, self.plans['configurations'][self.configuration]['batch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        oversample_foreground_percent=self.oversample_foreground_percent,
                                        sampling_probabilities=None, pad_sides=None)
        else:
            dl_tr = nnUNetDataLoader3D(dataset_tr, self.plans['configurations'][self.configuration]['batch_size'],
                                       initial_patch_size,
                                       self.plans['configurations'][self.configuration]['patch_size'],
                                       oversample_foreground_percent=self.oversample_foreground_percent,
                                       sampling_probabilities=None, pad_sides=None)
            dl_val = nnUNetDataLoader3D(dataset_val, self.plans['configurations'][self.configuration]['batch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        self.plans['configurations'][self.configuration]['patch_size'],
                                        oversample_foreground_percent=self.oversample_foreground_percent,
                                        sampling_probabilities=None, pad_sides=None)
        return dl_tr, dl_val

    @staticmethod
    def get_training_transforms(patch_size: Union[np.ndarray, Tuple[int]],
                                rotation_for_DA: dict,
                                deep_supervision_scales: Union[List, Tuple],
                                mirror_axes: Tuple[int, ...],
                                do_dummy_2d_data_aug: bool,
                                order_resampling_data: int = 1,
                                order_resampling_seg: int = 0,
                                border_val_seg: int = -1,
                                use_mask_for_norm: List[bool] = None,
                                is_cascaded: bool = False,
                                all_labels: Union[Tuple[int, ...], List[int]] = None,
                                regions: List[Union[List[int], Tuple[int, ...]]] = None) -> AbstractTransform:
        if is_cascaded and regions is not None:
            raise NotImplementedError('Region based training is not yet implemented for the cascade!')

        tr_transforms = []
        if do_dummy_2d_data_aug:
            ignore_axes = (0,)
            tr_transforms.append(Convert3DTo2DTransform())
            patch_size_spatial = patch_size[1:]
        else:
            patch_size_spatial = patch_size
            ignore_axes = None

        tr_transforms.append(SpatialTransform(
            patch_size_spatial, patch_center_dist_from_border=None,
            do_elastic_deform=False, alpha=(0, 0), sigma=(0, 0),
            do_rotation=True, angle_x=rotation_for_DA['x'], angle_y=rotation_for_DA['y'], angle_z=rotation_for_DA['z'],
            p_rot_per_axis=1,  # todo experiment with this
            do_scale=True, scale=(0.7, 1.4),
            border_mode_data="constant", border_cval_data=0, order_data=order_resampling_data,
            border_mode_seg="constant", border_cval_seg=border_val_seg, order_seg=order_resampling_seg,
            random_crop=False,  # random cropping is part of our dataloaders
            p_el_per_sample=0, p_scale_per_sample=0.2, p_rot_per_sample=0.2,
            independent_scale_for_each_axis=False  # todo experiment with this
        ))

        if do_dummy_2d_data_aug:
            tr_transforms.append(Convert2DTo3DTransform())

        tr_transforms.append(GaussianNoiseTransform(p_per_sample=0.1))
        tr_transforms.append(GaussianBlurTransform((0.5, 1.), different_sigma_per_channel=True, p_per_sample=0.2,
                                                   p_per_channel=0.5))
        tr_transforms.append(BrightnessMultiplicativeTransform(multiplier_range=(0.75, 1.25), p_per_sample=0.15))
        tr_transforms.append(ContrastAugmentationTransform(p_per_sample=0.15))
        tr_transforms.append(SimulateLowResolutionTransform(zoom_range=(0.5, 1), per_channel=True,
                                                            p_per_channel=0.5,
                                                            order_downsample=0, order_upsample=3, p_per_sample=0.25,
                                                            ignore_axes=ignore_axes))
        tr_transforms.append(GammaTransform((0.7, 1.5), True, True, retain_stats=True, p_per_sample=0.1))
        tr_transforms.append(GammaTransform((0.7, 1.5), False, True, retain_stats=True, p_per_sample=0.3))

        if len(mirror_axes) > 0:
            tr_transforms.append(MirrorTransform(mirror_axes))

        if use_mask_for_norm is not None and any(use_mask_for_norm):
            tr_transforms.append(MaskTransform([i for i in range(len(use_mask_for_norm)) if use_mask_for_norm[i]],
                                               mask_idx_in_seg=0, set_outside_to=0))

        tr_transforms.append(RemoveLabelTransform(-1, 0))

        if is_cascaded:
            assert all_labels is not None, 'We need all_labels for cascade augmentations'
            tr_transforms.append(MoveSegAsOneHotToData(1, all_labels, 'seg', 'data'))
            tr_transforms.append(ApplyRandomBinaryOperatorTransform(
                channel_idx=list(range(-len(all_labels), 0)),
                p_per_sample=0.4,
                key="data",
                strel_size=(1, 8),
                p_per_label=1))
            tr_transforms.append(
                RemoveRandomConnectedComponentFromOneHotEncodingTransform(
                    channel_idx=list(range(-len(all_labels), 0)),
                    key="data",
                    p_per_sample=0.2,
                    fill_with_other_class_p=0,
                    dont_do_if_covers_more_than_x_percent=0.15))

        tr_transforms.append(RenameTransform('seg', 'target', True))

        if regions is not None:
            tr_transforms.append(ConvertSegmentationToRegionsTransform(regions, 'target', 'target'))

        if deep_supervision_scales is not None:
            tr_transforms.append(DownsampleSegForDSTransform2(deep_supervision_scales, 0, input_key='target',
                                                              output_key='target'))
        tr_transforms.append(NumpyToTensor(['data', 'target'], 'float'))
        tr_transforms = Compose(tr_transforms)
        return tr_transforms

    @staticmethod
    def get_validation_transforms(deep_supervision_scales: Union[List, Tuple],
                                  is_cascaded: bool = False,
                                  all_labels: Union[Tuple[int, ...], List[int]] = None,
                                  regions: List[Union[List[int], Tuple[int, ...]]] = None) -> AbstractTransform:
        if is_cascaded and regions is not None:
            raise NotImplementedError('Region based training is not yet implemented for the cascade!')

        val_transforms = []
        val_transforms.append(RemoveLabelTransform(-1, 0))

        if is_cascaded:
            val_transforms.append(MoveSegAsOneHotToData(1, all_labels, 'seg', 'data'))

        val_transforms.append(RenameTransform('seg', 'target', True))

        if regions is not None:
            val_transforms.append(ConvertSegmentationToRegionsTransform(regions, 'target', 'target'))

        if deep_supervision_scales is not None:
                val_transforms.append(DownsampleSegForDSTransform2(deep_supervision_scales, 0, input_key='target',
                                                                   output_key='target'))

        val_transforms.append(NumpyToTensor(['data', 'target'], 'float'))
        val_transforms = Compose(val_transforms)
        return val_transforms

    def training_step(self, batch) -> STEP_OUTPUT:
        data = batch['data']
        target = batch['target']

        self.optimizer.zero_grad()

        output = self.network(data)

        # for now leave this untouched. For DDP we will have to change it (batch dice ;-) )
        l = self.loss(output, target)

        return l

    def training_epoch_end(self, outputs: EPOCH_OUTPUT) -> None:
        losses = torch.stack(outputs)
        losses = self.all_gather(losses)
        if self.trainer.is_global_zero:
            self.log('train_loss', torch.mean(losses), rank_zero_only=True)

    def validation_step(self, batch) -> Optional[STEP_OUTPUT]:
        data = batch['data']
        target = batch['target']

        self.optimizer.zero_grad()

        output = self.network(data)

        # for now leave this untouched. For DDP we will have to change it (batch dice ;-) )
        l = self.loss(output, target)

        # for keeping track of the dice substitute we need to compute the tp, fp and fn of each validation step
        # deep supervision, we only need the result at the highest resolution
        if isinstance(output, list):
            output = output[0]
            target = target[0]

        if self.regions is None:
            # we don't need softmax here because it doesn't change what value is the largest
            predicted_segmentation = output.argmax(1)
            # in our implementation the target is always a 4D or 5D tensor for 2d and 3d images, respectively
            # (shape b,c,x,y,z)
            target = target[:, 0]

            axes = tuple(range(1, len(target.shape)))
            num_classes = len(self.labels)

            tp_hard = torch.zeros((target.shape[0], num_classes - 1), device=output.device)
            fp_hard = torch.zeros((target.shape[0], num_classes - 1), device=output.device)
            fn_hard = torch.zeros((target.shape[0], num_classes - 1), device=output.device)

            for c in range(1, num_classes):
                tp_hard[:, c - 1] = sum_tensor((predicted_segmentation == c).float() * (target == c).float(), axes=axes)
                fp_hard[:, c - 1] = sum_tensor((predicted_segmentation == c).float() * (target != c).float(), axes=axes)
                fn_hard[:, c - 1] = sum_tensor((predicted_segmentation != c).float() * (target == c).float(), axes=axes)

        else:
            # we skip the sigmoid because sigmoid(x) > 0.5 is equivalent to x > 0
            predicted_segmentation = (output > 0).float()
            # here the predicted_segmentation is shape b,c,x,y,z where c containts the regions. The target has the same
            # shape
            axes = tuple(range(2, len(target.shape)))
            num_regions = len(self.regions)

            tp_hard = torch.zeros((target.shape[0], num_regions), device=output.device)
            fp_hard = torch.zeros((target.shape[0], num_regions), device=output.device)
            fn_hard = torch.zeros((target.shape[0], num_regions), device=output.device)

            for c in range(num_regions):
                tp_hard[:, c] = sum_tensor(predicted_segmentation[c] * target[c], axes=axes)
                fp_hard[:, c] = sum_tensor(predicted_segmentation[c] * (1 - target[c]), axes=axes)
                fn_hard[:, c] = sum_tensor((1 - predicted_segmentation[c] * target[c]), axes=axes)

        tp_hard = tp_hard.sum(0, keepdim=False).detach().cpu().numpy()
        fp_hard = fp_hard.sum(0, keepdim=False).detach().cpu().numpy()
        fn_hard = fn_hard.sum(0, keepdim=False).detach().cpu().numpy()

        return {'loss': l, 'tp_hard': tp_hard, 'fp_hard': fp_hard, 'fn_hard': fn_hard}

    def validation_epoch_end(self, outputs: EPOCH_OUTPUT) -> None:
        losses = torch.stack([i['loss'] for i in outputs])
        tps = torch.stack([i['tp_hard'] for i in outputs])
        fps = torch.stack([i['fp_hard'] for i in outputs])
        fns = torch.stack([i['fn_hard'] for i in outputs])

        # if we are multi-GPU we should all gather this shit
        tps = self.all_gather(tps).sum(0)
        fps = self.all_gather(fps).sum(0)
        fns = self.all_gather(fns).sum(0)

        dice_per_class_or_region = 2 * tps / (2 * tps + fps + fns)
        mean_fg_dice = torch.mean(dice_per_class_or_region)

        if self.trainer.is_global_zero:
            self.log('val_loss', torch.mean(losses), rank_zero_only=True)
            self.log('dice_per_class_or_region', torch.mean(dice_per_class_or_region), rank_zero_only=True)
            self.log('mean_fg_dice', torch.mean(mean_fg_dice), rank_zero_only=True)

    def on_epoch_start(self) -> None:
        sched = self.lr_schedulers()
        sched.step(self.current_epoch)

    def on_epoch_end(self) -> None:
        pass
