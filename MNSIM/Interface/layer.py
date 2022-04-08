#-*-coding:utf-8-*-
"""
@FileName:
    layer.py
@Description:
    base class for quantize layers
@Authors:
    Hanbo Sun(sun-hb17@mails.tsinghua.edu.cn)
@CreateTime:
    2022/04/07 14:37
"""
import abc
import copy
import functools
import math

import torch
import torch.nn.functional as F
from torch import nn
from torch.autograd import Function
from MNSIM.Interface.utils.component import Component

def _get_thres(bit_width):
    """
    get threshold for quantize
    """
    return 2 ** (bit_width - 1) - 1

def _get_cycle(bit_width, bit_split):
    """
    get bit cycle
    """
    return math.ceil((bit_width - 1) / bit_split)

class QuantizeFunction(Function):
    """
    quantize function user-defined
    """
    RATIO = 0.707
    @staticmethod
    def forward(ctx, inputs, quantize_cfg, last_bit_scale):
        """
        forward function
        quantize_cfg: mode, phase, bit
        last_bit_scale: first bit, last scale, not for range scale
        """
        # get scale
        if quantize_cfg["mode"] == "weight":
            scale = torch.max(torch.abs(inputs)).item()
        elif quantize_cfg["mode"] == "activation":
            r_scale = last_bit_scale[1].item() * _get_thres(last_bit_scale[0].item())
            if quantize_cfg["phase"] == "train":
                t_scale = (3*torch.std(inputs) + torch.abs(torch.mean(inputs))).item()
                if r_scale <= 0:
                    scale = t_scale
                else:
                    scale = QuantizeFunction.RATIO * r_scale + \
                        (1 - QuantizeFunction.RATIO) * t_scale
            elif quantize_cfg["phase"] == "test":
                scale = r_scale
            else:
                assert False, "phase should be train or test"
        else:
            assert False, "mode should be weight or activation"
        # quantize
        bit = quantize_cfg["bit"]
        thres = _get_thres(bit)
        output = inputs / (scale / thres)
        output = torch.clamp(torch.round(output), min=-thres, max=thres)
        output = output / (thres / scale)
        # save bit and scale, output
        last_bit_scale[0].fill_(bit)
        last_bit_scale[1].fill_(scale / thres)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output, None, None
Quantize = QuantizeFunction.apply

def split_by_bit(tensor, scale, bit_width, bit_split):
    """
    split tensor with scale and bit_width to bit_split
    """
    weight_cycle = _get_cycle(bit_width, bit_split) # weight cycle
    thres = _get_thres(bit_width) # threshold
    quantize_tensor = torch.clamp(torch.round(tensor / scale), min=-thres, max=thres)
    # split
    sign_tensor = torch.sign(quantize_tensor)
    abs_tensor = torch.abs(quantize_tensor)
    # traverse every bit_split
    t_weight = [0] + \
        [torch.fmod(abs_tensor, 2**(bit_split*(i+1))) for i in range(weight_cycle-1)] + \
        [abs_tensor]
    o_weight = [
        torch.mul(sign_tensor, (t_weight[i+1] - t_weight[i]) / (2**(bit_split*i)))
        for i in range(weight_cycle)
    ]
    return o_weight

class BaseWeightLayer(nn.Module, Component):
    """
    base class for quantize weight layers
    """
    REGISTRY = "weight_layer"
    def __init__(self, layer_ini):
        nn.Module.__init__(self)
        Component.__init__(self)
        # super(BaseWeightLayer, self).__init__()
        # copy layer_ini
        self.layer_ini = copy.deepcopy(layer_ini)
        # init attr
        self.input_split_num = None
        self.layer_list = None
        self.partial_func = None
        # get module and buffer list
        self.get_module_list()
        self.get_buffer_list()

    def get_module_list(self):
        """
        get module list for this layer
        """
        input_split_num, layer_config_list, layer_cls, partial_func = self.get_module_config()
        self.input_split_num = input_split_num
        self.partial_func = partial_func
        self.layer_list = nn.ModuleList([
            layer_cls(**layer_config) for layer_config in layer_config_list
        ])

    def get_buffer_list(self):
        """
        get buffer list for this layer
        """
        # set buffer list
        self.register_buffer("bit_scale_list", torch.FloatTensor([
            [self.layer_ini["quantize"]["input"], -1],
            [self.layer_ini["quantize"]["weight"], -1],
            [self.layer_ini["quantize"]["output"], -1],
        ]))

    @abc.abstractmethod
    def get_module_config(self):
        """
        get module config for this layer
        """
        raise NotImplementedError

    def forward(self, inputs, method="SINGLE_FIX_TEST", input_config=None):
        """
        forward for this layer
        """
        # different pass for different method
        # for method is TRADITION, we use traditional forward
        if method == "TRADITION":
            input_list = torch.split(inputs, self.input_split_num, dim=1)
            output_list = [l(v) for l, v in zip(self.layer_list, input_list)]
            return torch.sum(torch.stack(output_list, dim=0), dim=0)
        # for method is FIX_TRAIN, we use fix train forward
        if method == "FIX_TRAIN":
            weight_total = torch.cat([l.weight for l in self.layer_list], dim=1)
            quantize_weight =  Quantize(weight_total, {
                "mode": "weight",
                "phase": None,
                "bit": self.bit_scale_list[1, 0].item()
            }, self.bit_scale_list[1])
            output = self.partial_func(inputs, quantize_weight)
            quantize_output = Quantize(output, {
                "mode": "activation",
                "phase": "train" if self.training else "test",
                "bit": self.bit_scale_list[2, 0].item()
            }, self.bit_scale_list[2])
            return quantize_output
        # for method is SINGLE_FIX_TEST, we get weight and set weight forward
        if method == "SINGLE_FIX_TEST":
            assert not self.training, "method is SINGLE_FIX_TEST, but now is training"
            assert input_config is not None, "method is SINGLE_FIX_TEST, but input_config is None"
            self.bit_scale_list[0].copy_(input_config[0]) # for weight_layer, only be one input
            quantize_weight_bit_list = self.get_quantize_weight_bit_list()
            quantize_output = self.set_weights_forward(inputs, quantize_weight_bit_list)
            return quantize_output
        assert False, "method should be TRADITION, FIX_TRAIN or SINGLE_FIX_TEST"

    def get_quantize_weight_bit_list(self):
        """
        get quantize weight bit list
        """
        weight_scale = self.bit_scale_list[1, 1].item()
        weight_bit_width = self.bit_scale_list[1, 0].item()
        weight_bit_split = self.layer_ini["hardware"]["cell_bit"]
        quantize_weight_bit_list = [
            split_by_bit(l.weight, weight_scale, weight_bit_width, weight_bit_split)
            for l in self.layer_list
        ]
        return quantize_weight_bit_list

    def set_weights_forward(self, inputs, quantize_weight_bit_list):
        """
        set weights forward
        """
        assert not self.training, "function is set_weights_forward, but training"
        input_list = torch.split(inputs, self.input_split_num, dim=1)
        # for weight info
        weight_cycle = _get_cycle(
            self.bit_scale_list[1, 0].item(),
            self.layer_ini["hardware"]["cell_bit"]
        )
        # for input activation info
        input_cycle = _get_cycle(
            self.bit_scale_list[0, 0].item(),
            self.layer_ini["hardware"]["dac_bit"]
        )
        # for output activation info
        Q = self.layer_ini["hardware"]["adc_bit"]
        pf = self.layer_ini["hardware"]["point_shift"]
        output_scale = self.bit_scale_list[2, 1].item() * \
            _get_thres(self.bit_scale_list[2, 0].item())
        mul_scale = self.bit_scale_list[1, 1].item() * self.bit_scale_list[0, 1].item() * \
            (2 ** ((weight_cycle - 1)*self.layer_ini["hardware"]["cell_bit"])) * \
            (2 ** ((input_cycle - 1)*self.layer_ini["hardware"]["dac_bit"]))
        transfer_scale = 2 ** (pf + Q - 1)
        output_thres = _get_thres(Q)
        # accumulate output for layer, weight_cycle and input_cycle
        output_list = []
        for layer_num, module_weight_bit_list in enumerate(quantize_weight_bit_list):
            # for every module
            input_activation_bit_list = split_by_bit(input_list[layer_num], \
                self.bit_scale_list[0, 1].item(), self.bit_scale_list[0, 0].item(),
                self.layer_ini["hardware"]["dac_bit"],
            )
            # for every weight cycle and input cycle
            for i in range(input_cycle):
                for j in range(weight_cycle):
                    tmp = self.partial_func(input_activation_bit_list[i], module_weight_bit_list[j])
                    # scale for tmp, and quantize
                    tmp = tmp * (mul_scale / output_scale * transfer_scale)
                    tmp = torch.clamp(torch.round(tmp), min=-output_thres, max=output_thres)
                    # scale point for bit shift
                    scale_point = (input_cycle-1-i)*self.layer_ini["hardware"]["dac_bit"] + \
                        (weight_cycle-1-j)*self.layer_ini["hardware"]["cell_bit"]
                    tmp = tmp / (transfer_scale * (2**scale_point))
                    output_list.append(tmp)
        # sum output
        output = torch.sum(torch.stack(output_list, dim=0), dim=0)
        # quantize output
        output_thres = _get_thres(self.bit_scale_list[2, 0].item())
        output = torch.clamp(torch.round(output*output_thres), min=-output_thres, max=output_thres)
        return output / (output_thres / output_scale)


def split_by_num(num, base):
    """
    split base by num, e.g., 10 = [3, 3, 3, 1]
    """
    assert num > 0, "num should be greater than 0"
    assert base > 0, "base should be greater than 0"
    return [base] * (num // base) + [num % base] * (num % base > 0)

class QuantizeConv(BaseWeightLayer):
    """
    quantize conv layer
    """
    NAME = "conv"

    def get_module_config(self):
        """
        get module config for this conv layer
        return: input_split_num, layer_config_list, layer_cls, partial_func
        """
        # basic config for conv layer
        input_split_num = math.floor(self.layer_ini["hardware"]["xbar_row"] / \
            (self.layer_ini["layer"]["kernel_size"] ** 2))
        in_channels_list = split_by_num(
            self.layer_ini["layer"]["in_channels"],
            input_split_num
        )
        layer_config_list = [{
            "in_channels": in_channels,
            "out_channels": self.layer_ini["layer"]["out_channels"],
            "bias": False,
            "kernel_size": self.layer_ini["layer"]["kernel_size"],
            "stride": self.layer_ini["layer"].get("stride", 1),
            "padding": self.layer_ini["layer"].get("padding", 0),
        } for in_channels in in_channels_list]
        partial_func = functools.partial(F.conv2d, bias=None,\
            stride=layer_config_list[0]["stride"], padding=layer_config_list[0]["padding"]
        )
        return input_split_num, layer_config_list, nn.Conv2d, partial_func

class QuantizeFC(BaseWeightLayer):
    """
    quantize fc layer
    """
    NAME = "fc"

    def get_module_config(self):
        """
        get module config for this fc layer
        return: input_split_num, layer_config_list, layer_cls, partial_func
        """
        # basic config for fc layer
        input_split_num = self.layer_ini["hardware"]["xbar_row"]
        in_features_list = split_by_num(self.layer_ini["layer"]["in_features"], input_split_num)
        layer_config_list = [{
            "in_features": in_features,
            "out_features": self.layer_ini["layer"]["out_features"],
            "bias": False,
        } for in_features in in_features_list]
        partial_func = functools.partial(F.linear, bias=None)
        return input_split_num, layer_config_list, nn.Linear, partial_func
