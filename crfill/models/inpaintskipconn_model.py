import pdb
import warnings

import torch
import models.networks as networks
import util.util as util
from models.create_mask import MaskCreator
import random
import numpy as np


class InpaintskipconnModel(torch.nn.Module):
    @staticmethod
    def modify_commandline_options(parser, is_train):
        networks.modify_commandline_options(parser, is_train)
        parser.add_argument(
            "--path_objectshape_base", type=str, default="", help="path obj base"
        )
        parser.add_argument(
            "--path_objectshape_list", type=str, default="", help="path obj list"
        )
        parser.add_argument(
            "--update_part", type=str, default="all", help="update part"
        )
        parser.add_argument(
            "--d_mask_in", action="store_true", help="if specified, d mask in"
        )
        parser.add_argument(
            "--no_fine_loss",
            action="store_true",
            help="if specified, do *not* use refinementstageloss",
        )
        parser.add_argument(
            "--load_pretrained_g", type=str, required=False, help="load pt g"
        )
        parser.add_argument(
            "--load_pretrained_d", type=str, required=False, help="load pt d"
        )
        parser.add_argument(
            "--fastercnn_loc",
            default="models/fasterrcnn.pth",
            help="location of the fasterrcnn.pth",
        )
        parser.add_argument("--no_sharp_skip_connection", action="store_true")
        parser.add_argument("--generator_weight", type=float, default=1)
        parser.add_argument("--discriminator_weight", type=float, default=1)
        return parser

    def __init__(self, opt):
        super().__init__()
        self.opt = opt

        self.FloatTensor = (
            torch.cuda.FloatTensor if self.use_gpu() else torch.FloatTensor
        )
        self.ByteTensor = torch.cuda.ByteTensor if self.use_gpu() else torch.ByteTensor

        self.netG, self.netD, self.netDRCNN = self.initialize_networks(opt)
        if opt.isTrain and opt.load_pretrained_g is not None:
            print(f"looad {opt.load_pretrained_g}")
            self.netG = util.load_network_path(self.netG, opt.load_pretrained_g)
        if opt.isTrain and opt.load_pretrained_d is not None:
            print(f"looad {opt.load_pretrained_d}")
            self.netD = util.load_network_path(self.netD, opt.load_pretrained_d)

        # set loss functions
        if opt.isTrain:
            # self.mask_creator = MaskCreator(opt.path_objectshape_list, opt.path_objectshape_base)
            self.criterionGAN = networks.GANLoss(
                opt.gan_mode, tensor=self.FloatTensor, opt=self.opt
            )
            self.criterionFeat = torch.nn.L1Loss()
            if opt.vgg_loss:
                self.criterionVGG = networks.VGGLoss(self.opt.gpu_ids)

    # Entry point for all calls involving forward pass
    # of deep networks. We used this approach since DataParallel module
    # can't parallelize custom functions, we branch to different
    # routines based on |mode|.
    def forward(self, data, mode):
        negative, positive, mask, crop_bbox, lesion_bbox, cxr = self.preprocess_input(
            data
        )

        if mode == "generator":
            g_loss, coarse_image, composed_image = self.compute_generator_loss(
                negative, positive, mask, cxr
            )
            generated = {"coarse": coarse_image, "composed": composed_image}
            return g_loss, negative, generated
        elif mode == "discriminator":
            d_loss = self.compute_discriminator_loss(negative, positive, mask)
            return d_loss, data["inputs"]
        elif mode == "inference":
            with torch.no_grad():
                coarse_addition, fake_addition = self.generate_fake(negative, mask)
                composed_image = self.place_addition_on_cxr(
                    fake_addition, negative, mask
                )
            return composed_image, negative
        else:
            raise ValueError("|mode| is invalid")

    def create_optimizers(self, opt):
        G_params = self.netG.get_param_list(opt.update_part)
        # G_params = [p for name, p in self.netG.named_parameters() \
        #        if (not name.startswith("coarse"))]
        if opt.isTrain:
            D_params = list(self.netD.parameters())

        beta1, beta2 = opt.beta1, opt.beta2
        if opt.no_TTUR:
            G_lr, D_lr = opt.lr, opt.lr
        else:
            G_lr, D_lr = opt.lr / 2, opt.lr * 2

        optimizer_G = torch.optim.Adam(G_params, lr=G_lr, betas=(beta1, beta2))
        optimizer_D = torch.optim.Adam(D_params, lr=D_lr, betas=(beta1, beta2))

        return optimizer_G, optimizer_D

    def save(self, epoch):
        util.save_network(self.netG, "G", epoch, self.opt)
        util.save_network(self.netD, "D", epoch, self.opt)

    ############################################################################
    # Private helper methods
    ############################################################################

    def initialize_networks(self, opt):
        netG = networks.define_G(opt)
        if opt.isTrain:
            netD = networks.define_D(opt)
            netDRCNN = networks.define_DRCNN(opt)
        else:
            netD = None
            netDRCNN = None

        if not opt.isTrain or opt.continue_train:
            netG = util.load_network(netG, "G", opt.which_epoch, opt)
            if opt.isTrain:
                netD = util.load_network(netD, "D", opt.which_epoch, opt)
                raise NotImplementedError(
                    "Loading DRCNN for continuing training not implemented yet"
                )
        return netG, netD, netDRCNN

    # preprocess the input, such as moving the tensors to GPUs and
    # transforming the label map to one-hot encoding
    # |data|: dictionary of the input data

    def preprocess_input(self, data):
        if (
            self.use_gpu()
        ):  # this command is irrelevant, all data will already be at the GPU due to DataParallel in pix2pixtrainer
            data["neg_cropped_normalized_cxr"] = data[
                "neg_cropped_normalized_cxr"
            ].cuda()
            data["pos_cropped_normalized_cxr"] = data[
                "pos_cropped_normalized_cxr"
            ].cuda()
            data["neg_cropped_mask"] = data["neg_cropped_mask"].cuda()

        return (
            data["neg_cropped_normalized_cxr"],
            data["pos_cropped_normalized_cxr"],
            data["neg_cropped_mask"],
            data["neg_crop_bbox"],
            data["neg_lesion_bbox"],
            data["neg_cxr"],
        )

    def g_image_loss(
        self,
        coarse_image,
        negative,
        composed_image,
        positive,
        mask,
        composed_full_image,
        full_image_bbox,
    ):
        G_losses = {}

        # Faster RCNN loss -- For aux loss, composed_full_image and full_image_bbox will be passed as None
        if composed_full_image is not None:
            rcnn_losses = self.RCNN_loss(composed_full_image, full_image_bbox)
            G_losses["RCNN"] = rcnn_losses
            # G_losses['RCNN'] = self.criterionGAN(pred_rcnn, True, for_discriminator=False)

        # 'Normal' Adversarial
        if not self.opt.no_gan_loss and not self.opt.no_fine_loss:
            pred_fake, pred_real = self.discriminate(composed_image, positive, mask)

            G_losses["GAN"] = (
                self.criterionGAN(pred_fake, True, for_discriminator=False)
                * self.opt.generator_weight
            )

        if self.opt.vgg_loss and not self.opt.no_fine_loss:
            G_losses["VGG"] = (
                self.criterionVGG(negative, positive) * self.opt.lambda_vgg
            )
        if not self.opt.no_l1_loss:
            if coarse_image is not None:
                G_losses["L1_coarse"] = (
                    torch.nn.functional.l1_loss(coarse_image, negative)
                    * self.opt.beta_l1
                )
            if not self.opt.no_fine_loss:
                G_losses["L1_fine"] = (
                    torch.nn.functional.l1_loss(composed_image, negative)
                    * self.opt.beta_l1
                )
        return G_losses

    def place_addition_on_cxr(self, addition, starting_cxr, mask):
        if not self.opt.no_sharp_skip_connection:
            return (starting_cxr*torch.exp(addition)) * mask + starting_cxr * (1 - mask)

    def compute_generator_loss(
        self, negative, positive, mask, crop_bbox, lesion_bbox, cxr
    ):
        return NotImplementedError

    def compute_discriminator_loss(self, negative, positive, desired_mask):
        D_losses = {}
        if not self.opt.no_gan_loss:
            with torch.no_grad():
                coarse_add, fine_add = self.generate_fake(negative, desired_mask)
                fine_add = fine_add.detach()
                fine_add.requires_grad_()
                composed_image = self.place_addition_on_cxr(
                    fine_add, negative, desired_mask
                )

            pred_fake, pred_real = self.discriminate(
                composed_image, positive, desired_mask
            )

            D_losses["D_Fake"] = self.criterionGAN(
                pred_fake, False, for_discriminator=True
            )*self.opt.discriminator_weight
            D_losses["D_real"] = self.criterionGAN(
                pred_real, True, for_discriminator=True
            )*self.opt.discriminator_weight

        return D_losses

    def generate_fake(self, inputs, mask):
        coarse_image, fake_image = self.netG(inputs, mask)

        return coarse_image, fake_image

    # Given fake and real image, return the prediction of discriminator
    # for each fake and real image.

    def discriminate(self, fake_image, real_image, mask):
        fake_concat = fake_image
        real_concat = real_image
        # In Batch Normalization, the fake and real images are
        # recommended to be in the same batch to avoid disparate
        # statistics in fake and real images.
        # So both fake and real images are fed to D all at once.
        fake_and_real = torch.cat([fake_concat, real_concat], dim=0)
        if self.opt.d_mask_in:
            mask_all = torch.cat([mask, mask], dim=0)
        else:
            mask_all = None

        discriminator_out = self.netD(fake_and_real, mask_all)

        pred_fake, pred_real = self.divide_pred(discriminator_out)

        return pred_fake, pred_real

    def RCNN_loss(self, composed_image, ground_truth_bbox):
        targets = [
            {"boxes": torch.unsqueeze(bbox, 0), "labels": torch.LongTensor([1]).cuda()}
            for bbox in ground_truth_bbox
        ]  # label=1 for tumor class
        loss_dict = self.netDRCNN(composed_image, targets)

        _loss_sum = 0
        count = 0
        for key in loss_dict:
            count += 1
            _loss_sum += loss_dict[key]
        # TODO: think about what (sum of) loss is the correct one to return
        return _loss_sum

    # Take the prediction of fake and real images from the combined batch
    def divide_pred(self, pred):
        # the prediction contains the intermediate outputs of multiscale GAN,
        # so it's usually a list
        if type(pred) == list:
            fake = []
            real = []
            for p in pred:
                fake.append([tensor[: tensor.size(0) // 2] for tensor in p])
                real.append([tensor[tensor.size(0) // 2 :] for tensor in p])
        else:
            fake = pred[: pred.size(0) // 2]
            real = pred[pred.size(0) // 2 :]

        return fake, real

    def get_edges(self, t):
        edge = self.ByteTensor(t.size()).zero_()
        edge[:, :, :, 1:] = edge[:, :, :, 1:] | (t[:, :, :, 1:] != t[:, :, :, :-1])
        edge[:, :, :, :-1] = edge[:, :, :, :-1] | (t[:, :, :, 1:] != t[:, :, :, :-1])
        edge[:, :, 1:, :] = edge[:, :, 1:, :] | (t[:, :, 1:, :] != t[:, :, :-1, :])
        edge[:, :, :-1, :] = edge[:, :, :-1, :] | (t[:, :, 1:, :] != t[:, :, :-1, :])
        return edge.float()

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return eps.mul(std) + mu

    def use_gpu(self):
        return len(self.opt.gpu_ids) > 0
