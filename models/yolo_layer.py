import sys
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.append('../')

from utils.torch_utils import to_cpu
from utils.iou_rotated_boxes_utils import iou_pred_vs_target_boxes, iou_rotated_boxes_targets_vs_anchors, \
    get_polygons_areas_fix_xyz


class YoloLayer(nn.Module):
    """Yolo layer"""

    def __init__(self, anchors, num_classes, stride, ignore_thresh):
        super(YoloLayer, self).__init__()
        # Update the attributions when parsing the cfg during create the darknet
        self.num_classes = num_classes
        self.anchors = anchors
        self.num_anchors = len(anchors)
        self.ignore_thres = ignore_thresh
        self.stride = stride
        # self.scale_x_y = scale_x_y
        # self.mse_loss = nn.MSELoss()
        # self.bce_loss = nn.BCELoss()
        self.noobj_scale = 100
        self.obj_scale = 1
        self.lgiou_scale = 3.54
        self.leular_scale = 3.54
        self.lobj_scale = 64.3
        self.lcls_scale = 37.4

        self.seen = 0
        # Initialize dummy variables
        self.grid_size = 0
        self.img_size = 0
        self.metrics = {}
        self.num_b_b_attr = 9

    def compute_grid_offsets(self, grid_size):
        self.grid_size = grid_size
        g = self.grid_size
        self.stride = self.img_size / self.grid_size
        # Calculate offsets for each grid
        self.grid_x = torch.arange(g, device=self.device, dtype=torch.float).repeat(g, 1).view([1, 1, g, g])
        self.grid_y = torch.arange(g, device=self.device, dtype=torch.float).repeat(g, 1).t().view([1, 1, g, g])
        self.scaled_anchors = torch.tensor(
            [(a_h / self.stride, a_w / self.stride, a_l / self.stride, im, re) 
             for a_h, a_w, a_l, im, re in
             self.anchors], device=self.device, dtype=torch.float)
        self.anchor_w = self.scaled_anchors[:, 1:2].view((1, self.num_anchors, 1, 1))
        self.anchor_h = self.scaled_anchors[:, 0:1].view((1, self.num_anchors, 1, 1))
        self.anchor_l = self.scaled_anchors[:, 2:3].view((1, self.num_anchors, 1, 1))

        # Pre compute polygons and areas of anchors
        self.scaled_anchors_polygons, self.sa_volumes, self.sa_low_hs, self.sa_high_hs = get_polygons_areas_fix_xyz(
            self.scaled_anchors)

    def build_targets(self, pred_boxes, pred_cls, target, anchors, ignore_thres):
        """ Built yolo targets to compute loss
        :param out_boxes: [num_samples or batch, num_anchors, grid_size, grid_size, self.num_b_b_attr-3]
        :param pred_cls: [num_samples or batch, num_anchors, grid_size, grid_size, num_classes]
        :param target: [num_boxes, self.num_b_b_attr-1]
        :param anchors: [num_anchors, 4]
        :return:
        """
        nB, nA, nG, _, nC = pred_cls.size()
        n_target_boxes = target.size(0)

        # Create output tensors on "device"
        obj_mask = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.uint8)
        noobj_mask = torch.full(size=(nB, nA, nG, nG), fill_value=1, device=self.device, dtype=torch.uint8)
        class_mask = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        iou_scores = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tx = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        ty = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tz = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tw = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        th = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tl = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tim = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tre = torch.full(size=(nB, nA, nG, nG), fill_value=0, device=self.device, dtype=torch.float)
        tcls = torch.full(size=(nB, nA, nG, nG, nC), fill_value=0, device=self.device, dtype=torch.float)
        giou_loss = torch.tensor([0.], device=self.device, dtype=torch.float)

        if n_target_boxes > 0:  # Make sure that there is at least 1 box
            # scale up x, y, h, w, l, concatenate with im,re
            target_boxes = torch.cat((target[:, 2:4] * nG, target[:, 4:5], target[:, 5:self.num_b_b_attr-1] * nG, target[:, self.num_b_b_attr-1:10]), dim=-1)

            gxyz = target_boxes[:, :3]
            ghwl = target_boxes[:, 3:self.num_b_b_attr-3]
            gimre = target_boxes[:, self.num_b_b_attr-3:self.num_b_b_attr-1]

            tg_polygons, tg_volumes, tg_low_hs, tg_high_hs = get_polygons_areas_fix_xyz(target_boxes[:, 3:self.num_b_b_attr-1])
            # Get anchors with best iou
            ious_a_tg = iou_rotated_boxes_targets_vs_anchors(self.scaled_anchors_polygons, self.sa_volumes, self.sa_low_hs,
                                                             self.sa_high_hs, tg_polygons, tg_volumes, tg_low_hs,
                                                             tg_high_hs)
            best_ious, best_n = ious_a_tg.max(0)
            b, target_labels = target[:, :2].long().t()

            gx, gy, gz = gxyz.t()
            gh, gw, gl = ghwl.t()
            gim, gre = gimre.t()
            gi, gj, _ = gxyz.long().t()
            # Set masks
            obj_mask[b, best_n, gj, gi] = 1
            noobj_mask[b, best_n, gj, gi] = 0

            # Set noobj mask to zero where iou exceeds ignore threshold
            for i, anchor_ious in enumerate(ious_a_tg.t()):
                noobj_mask[b[i], anchor_ious > self.ignore_thres, gj[i], gi[i]] = 0

            # Coordinates
            tx[b, best_n, gj, gi] = gx - gx.floor()
            ty[b, best_n, gj, gi] = gy - gy.floor()
            tz[b, best_n, gj, gi] = gz
            # Width and height
            tw[b, best_n, gj, gi] = torch.log(gw / anchors[best_n][:, 1] + 1e-16)
            th[b, best_n, gj, gi] = torch.log(gh / anchors[best_n][:, 0] + 1e-16)
            tl[b, best_n, gj, gi] = torch.log(gl / anchors[best_n][:, 2] + 1e-16)
            # Im and real part
            tim[b, best_n, gj, gi] = gim
            tre[b, best_n, gj, gi] = gre

            # One-hot encoding of label
            tcls[b, best_n, gj, gi, target_labels] = 1
            # Compute label correctness and iou at best anchor
            class_mask[b, best_n, gj, gi] = (pred_cls[b, best_n, gj, gi].argmax(-1) == target_labels).float()
            ious_pred_tg, giou_loss = iou_pred_vs_target_boxes(pred_boxes[b, best_n, gj, gi], target_boxes,
                                                       GIoU=self.use_giou_loss)
            iou_scores[b, best_n, gj, gi] = ious_pred_tg
            if self.reduction == 'mean':
                giou_loss /= n_target_boxes
            # tconf = obj_mask.float()
            
        # return iou_scores, giou_loss, class_mask, obj_mask.type(torch.bool), noobj_mask.type(torch.bool), \
        tconf = obj_mask.float()
        obj_mask = obj_mask.type(torch.bool)
        noobj_mask = noobj_mask.type(torch.bool)

        return iou_scores, class_mask, obj_mask, noobj_mask, \
                tx, ty, tz, th, tw, tl, tim, tre, tcls, tconf

    def forward(self, x, targets=None, img_size=608, use_giou_loss=False):
        """
        :param x: [num_samples or batch, num_anchors * (self.num_b_b_attr + num_classes), grid_size, grid_size]
        :param targets: [num boxes, self.num_b_b_attr] (box_idx, class, x, y, z, h, w, l, yaw)
        :param img_size: default 608
        :return:
        """
        self.img_size = img_size
        self.use_giou_loss = use_giou_loss
        self.device = x.device
        
        # num_samples, _, _, grid_size = x.size()

        # prediction = x.view(num_samples, self.num_anchors, self.num_classes + self.num_b_b_attr, grid_size, grid_size)
        # prediction = prediction.permute(0, 1, 3, 4, 2).contiguous()
        # prediction size: [num_samples, num_anchors, grid_size, grid_size, num_classes + self.num_b_b_attr]
        
        num_samples = x.size(0)
        grid_size = x.size(2)

        prediction = (
            x.view(num_samples, self.num_anchors, self.num_classes + self.num_b_b_attr, grid_size, grid_size)
            .permute(0, 1, 3, 4, 2)
            .contiguous()
        )

        # Get outputs
        pred_x = torch.sigmoid(prediction[..., 0])  # Center x
        pred_y = torch.sigmoid(prediction[..., 1])  # Center y
        pred_z = torch.sigmoid(prediction[..., 2])
        pred_w = prediction[..., 4]  # Width
        pred_h = prediction[..., 3]  # Height
        pred_l = prediction[..., 5]  # Length
        pred_im = prediction[..., self.num_b_b_attr-3]  # angle imaginary part (range: 0 to 1)
        pred_re = prediction[..., self.num_b_b_attr-2]  # angle real part (range: 0 to 1)
        pred_conf = torch.sigmoid(prediction[..., self.num_b_b_attr-1])  # Conf
        pred_cls = torch.sigmoid(prediction[..., self.num_b_b_attr:])  # Cls pred.

        # If grid size does not match current we compute new offsets
        if grid_size != self.grid_size:
            self.compute_grid_offsets(grid_size)

        # Add offset and scale with anchors
        # pred_boxes size: [num_samples, num_anchors, grid_size, grid_size, self.num_b_b_attr-3]
        pred_boxes = torch.empty(prediction[..., :self.num_b_b_attr-1].shape, device=self.device, dtype=torch.float)
        pred_boxes[..., 0] = pred_x + self.grid_x
        pred_boxes[..., 1] = pred_y + self.grid_y
        pred_boxes[..., 2] = pred_z  # Only 1 grid

        pred_boxes[..., 4] = torch.exp(pred_w).clamp(max=1E3) * self.anchor_w
        pred_boxes[..., 3] = torch.exp(pred_h).clamp(max=1E3) * self.anchor_h
        pred_boxes[..., 5] = torch.exp(pred_l).clamp(max=1E3) * self.anchor_l
        pred_boxes[..., self.num_b_b_attr-3] = pred_im
        pred_boxes[..., self.num_b_b_attr-2] = pred_re

        output = torch.cat(
            (
                pred_boxes[..., :2].view(num_samples, -1, 2) * self.stride,  # x, y
                pred_boxes[..., 2:3].view(num_samples, -1, 1),  # z
                pred_boxes[..., 3:self.num_b_b_attr-3].view(num_samples, -1, 3) * self.stride,  # h, w, l
                pred_boxes[..., self.num_b_b_attr-3:self.num_b_b_attr-1].view(num_samples, -1, 2),  # im, re
                pred_conf.view(num_samples, -1, 1),  # conf
                pred_cls.view(num_samples, -1, self.num_classes),  # classes
            ),
            dim=-1
        )
        # output size: [num_samples, num boxes, self.num_b_b_attr + num_classes]

        if targets is None:
            return output, 0
        else:
            self.reduction = 'mean'
            iou_scores, class_mask, obj_mask, noobj_mask, tx, ty, tz, th, tw, tl, tim, tre, tcls, tconf = self.build_targets(
                pred_boxes=pred_boxes,
                pred_cls=pred_cls,
                target=targets,
                anchors=self.scaled_anchors,
                ignore_thres=self.ignore_thres,
            )

            loss_x = F.mse_loss(pred_x[obj_mask], tx[obj_mask], reduction=self.reduction)
            loss_y = F.mse_loss(pred_y[obj_mask], ty[obj_mask], reduction=self.reduction)
            loss_z = F.mse_loss(pred_z[obj_mask], tz[obj_mask], reduction=self.reduction)

            loss_w = F.mse_loss(pred_w[obj_mask], tw[obj_mask], reduction=self.reduction)
            loss_h = F.mse_loss(pred_h[obj_mask], th[obj_mask], reduction=self.reduction)
            loss_l = F.mse_loss(pred_l[obj_mask], tl[obj_mask], reduction=self.reduction)

            loss_im = F.mse_loss(pred_im[obj_mask], tim[obj_mask], reduction=self.reduction)
            loss_re = F.mse_loss(pred_re[obj_mask], tre[obj_mask], reduction=self.reduction)
            loss_eular = loss_im + loss_re

            loss_conf_obj = F.binary_cross_entropy(pred_conf[obj_mask], tconf[obj_mask], reduction=self.reduction)
            loss_conf_noobj = F.binary_cross_entropy(pred_conf[noobj_mask], tconf[noobj_mask], reduction=self.reduction)
            loss_cls = F.binary_cross_entropy(pred_cls[obj_mask], tcls[obj_mask], reduction=self.reduction)

            if self.use_giou_loss:
                loss_obj = loss_conf_obj + loss_conf_noobj
                total_loss = giou_loss * self.lgiou_scale + loss_obj * self.lobj_scale + loss_cls * self.lcls_scale
            else:
                loss_obj = self.obj_scale * loss_conf_obj + self.noobj_scale * loss_conf_noobj
                total_loss = loss_x + loss_y + loss_z + loss_w + loss_h + loss_l + loss_eular + loss_obj + loss_cls

            # Metrics (store loss values using tensorboard)
            cls_acc = 100 * class_mask[obj_mask].mean()
            conf_obj = pred_conf[obj_mask].mean()
            conf_noobj = pred_conf[noobj_mask].mean()
            conf50 = (pred_conf > 0.5).float()
            iou50 = (iou_scores > 0.5).float()
            iou75 = (iou_scores > 0.75).float()
            detected_mask = conf50 * class_mask * tconf
            precision = torch.sum(iou50 * detected_mask) / (conf50.sum() + 1e-16)
            recall50 = torch.sum(iou50 * detected_mask) / (obj_mask.sum() + 1e-16)
            recall75 = torch.sum(iou75 * detected_mask) / (obj_mask.sum() + 1e-16)

            self.metrics = {
                "loss": to_cpu(total_loss).item(),
                "loss_x": to_cpu(loss_x).item(),
                "loss_y": to_cpu(loss_y).item(),
                "loss_z": to_cpu(loss_z).item(),
                "loss_w": to_cpu(loss_w).item(),
                "loss_h": to_cpu(loss_h).item(),
                "loss_l": to_cpu(loss_l).item(),
                "loss_im": to_cpu(loss_im).item(),
                "loss_re": to_cpu(loss_re).item(),
                "loss_obj": to_cpu(loss_obj).item(),
                "loss_cls": to_cpu(loss_cls).item(),
                "cls_acc": to_cpu(cls_acc).item(),
                "recall50": to_cpu(recall50).item(),
                "recall75": to_cpu(recall75).item(),
                "precision": to_cpu(precision).item(),
                "conf_obj": to_cpu(conf_obj).item(),
                "conf_noobj": to_cpu(conf_noobj).item(),
                "grid_size": grid_size,
            }

            return output, total_loss
