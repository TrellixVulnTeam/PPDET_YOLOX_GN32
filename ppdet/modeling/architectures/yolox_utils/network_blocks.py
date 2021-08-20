import paddle
from paddle import nn


class SiLU(nn.Layer):
    @staticmethod
    def forward(x):
        return x * nn.functional.sigmoid(x)


def get_activation(name="silu", inplace=True):
    # paddle does not have inplace !!!
    if name == "silu":
        module = nn.Silu()
    elif name == "relu":
        module = nn.ReLU()
    elif name == "lrelu":
        module = nn.LeakyReLU(0.1)
    else:
        raise AttributeError("Unsupported act type: {}".format(name))
    return module

#CBA封装 conv+bn+act
class BaseConv(nn.Layer):
    """ [Conv2d]-[BN]-[activation] """

    def __init__(self, in_channels, out_channels,
                 kernel_size,
                 stride,
                 groups=1,
                 bias=False,
                 activation="silu"):
        super().__init__()
        pad = (kernel_size - 1) // 2
        self.conv = nn.Conv2D(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=pad,
            groups=groups,
            bias_attr=bias,
        )

        weight_attr = paddle.ParamAttr(regularizer=paddle.regularizer.L2Decay(0))
        bias_attr = paddle.ParamAttr(regularizer=paddle.regularizer.L2Decay(0))
        self.bn = nn.BatchNorm2D(num_features=out_channels, epsilon=1e-3, momentum=0.97, weight_attr=weight_attr, bias_attr=bias_attr)
        # self.bn = nn.BatchNorm2D(num_features=out_channels)
        self.act = get_activation(activation, inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.act(x)
        return x

    def fuse_forward(self, x):
        x = self.conv(x)
        x = self.act(x)
        return x


# 深度可分离卷积
class DWConv(nn.Layer):
    """ Depthwise Conv + Conv """

    def __init__(self, in_channels, out_channels,
                 kernel_size, stride=1, activation="silu"):
        super().__init__()

        self.dconv = BaseConv(in_channels=in_channels,
                              out_channels=in_channels,
                              kernel_size=kernel_size,
                              stride=stride,
                              groups=in_channels,
                              activation=activation)

        self.pconv = BaseConv(in_channels=in_channels,
                              out_channels=out_channels,
                              kernel_size=1,
                              stride=1,
                              groups=1,
                              activation=activation)

    def forward(self, x):
        x = self.dconv(x)
        x = self.pconv(x)
        return x


#瓶颈模块
class Bottleneck(nn.Layer):
    def __init__(self, in_channels, out_channels,
                 shortcut=True,
                 expansion=0.5,
                 depthwise=False,
                 activation='silu'):

        super().__init__()
        hidden_channels = int(out_channels * expansion)

        self.conv_1 = BaseConv(in_channels=in_channels,
                               out_channels=hidden_channels,
                               kernel_size=1,
                               stride=1,
                               activation=activation)

        Conv = DWConv if depthwise else BaseConv
        self.conv_2 = Conv(in_channels=hidden_channels,
                           out_channels=out_channels,
                           kernel_size=3,
                           stride=1,
                           activation=activation)
        self.use_add = shortcut and in_channels == out_channels

        # print(depthwise, expansion, shortcut, activation)
        self.depthwise = depthwise

    def forward(self, x, flag=""):
        # x = self.conv_1(x) #修改了x有问题
        # if flag == "debug":
        #     print("saving...", self.depthwise)
        #     print(self.conv_2)
        #     import numpy as np
        #     print(np.save("/f/tmp_rida_report/paddle_yolox", x.numpy()))
        #     exit()



        # y = self.conv_2(x)
        # print("saving...", self.depthwise, self.use_add)
        # if flag == "debug":
        #     import numpy as np
        #     print(np.save("/f/tmp_rida_report/paddle_yolox", y.numpy()))
        #     exit()

        y = self.conv_2(self.conv_1(x))

        if self.use_add:
            y = y + x
        return y


# Resnet层
class ResLayer(nn.Layer):
    def __init__(self, in_channels):
        super().__init__()
        mid_channels = in_channels // 2

        self.layer_1 = BaseConv(in_channels=in_channels,
                                out_channels=mid_channels,
                                kernel_size=1,
                                stride=1,
                                activation='lrelu')

        self.layer_2 = BaseConv(in_channels=mid_channels,
                                out_channels=in_channels,
                                kernel_size=3,
                                stride=1,
                                activation='lrelu')

    def forward(self, x):
        y = self.layer_2(self.layer_1(x))
        return x + y


# YoloV3的SPP层
class SPPBottleneck(nn.Layer):
    """ Spatial Pyramid Pooling - in YOLOv3-SPP """
    def __init__(self, in_channels, out_channels,
                 kernel_sizes=(5, 9, 13),
                 activation='silu'):
        super().__init__()
        hidden_channels = in_channels // 2
        self.conv_1 = BaseConv(in_channels=in_channels,
                               out_channels=hidden_channels,
                               kernel_size=1,
                               stride=1,
                               activation=activation)
        m = [nn.MaxPool2D(kernel_size=ks,
                          stride=1,
                          padding=ks//2) for ks in kernel_sizes] #最大池化+paddin+stride保证输出维度一致
        self.m = nn.LayerList(m)
        conv2_channels = hidden_channels * (len(kernel_sizes) + 1)
        self.conv_2 = BaseConv(in_channels=conv2_channels,
                               out_channels=out_channels,
                               kernel_size=1,
                               stride=1,
                               activation=activation)

    def forward(self, x):
        x = self.conv_1(x)
        x = paddle.concat([x] + [m(x) for m in self.m], axis=1)
        x = self.conv_2(x)
        return x

#CSP层
class CSPLayer(nn.Layer):
    """ C3 in YOLOv5, CSP Bottleneck with 3 conv """
    def __init__(self, in_channels, out_channels,
                 bottleneck_cnt=1,
                 shortcut=True,
                 expansion=0.5,
                 depthwise=False,
                 activation='silu'):
        super().__init__()
        hidden_channels = int(out_channels * expansion)

        self.conv_1 = BaseConv(in_channels=in_channels,
                               out_channels=hidden_channels,
                               kernel_size=1,
                               stride=1,
                               activation=activation)

        self.conv_2 = BaseConv(in_channels=in_channels,
                               out_channels=hidden_channels,
                               kernel_size=1,
                               stride=1,
                               activation=activation)

        self.conv_3 = BaseConv(in_channels=2*hidden_channels,
                               out_channels=out_channels,
                               kernel_size=1,
                               stride=1,
                               activation=activation)

        m = [Bottleneck(in_channels=hidden_channels,
                        out_channels=hidden_channels,
                        shortcut=shortcut,
                        expansion=1.0,
                        depthwise=depthwise,
                        activation=activation) for _ in range(bottleneck_cnt)]

        self.m = nn.Sequential(*m)


    def forward(self, x, flag=""):
        x1 = self.conv_1(x)
        x2 = self.conv_2(x)



        x1 = self.m(x1) #bug

        # # if flag == "debug":
        # #     print(self.short_cut)
        # #     x1= self.bottle_1(x1)
        # #     import numpy as np
        # #     print(np.save("/f/tmp_rida_report/paddle_yolox", x1.detach().cpu().numpy()))
        # #     exit()
        #
        # for i in range(len(self.m)):
        #     if flag == "debug":
        #         x1 = self.m[i](x1, flag if i == 3 else "")
        #     else:
        #         x1 = self.m[i](x1)
        #     # if i == 1 and flag == "debug":
        #         # print(self.m[i])
        #         # import numpy as np
        #         # print(np.save("/f/tmp_rida_report/paddle_yolox", x1.numpy()))
        #         # exit()

        x = paddle.concat((x1, x2), axis=1)

        out = self.conv_3(x)

        return out

#Focus 模块主要是实现没有信息丢失的下采样
class Focus(nn.Layer):
    """ Focus width and height information into channel space """
    def __init__(self, in_channels, out_channels,
                 kernel_size=1, stride=1, activation='silu'):
        super().__init__()
        self.conv = BaseConv(in_channels=in_channels*4,
                             out_channels=out_channels,
                             kernel_size=kernel_size,
                             stride=stride,
                             activation=activation)

    def forward(self, x):
        # todo: double check tensor shape  [?]
        # shape x (b,c,w,h) -> y (b,4c,w/2,h/2)


        patch_top_left = x[:, :, ::2, ::2]
        patch_top_right = x[:, :, ::2, 1::2]
        patch_bot_left = x[:, :, 1::2, ::2]
        patch_bot_right = x[:, :, 1::2, 1::2]
        x = paddle.concat((patch_top_left,
                           patch_bot_left,
                           patch_top_right,
                           patch_bot_right), axis=1)
        return self.conv(x)
