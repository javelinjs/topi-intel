from __future__ import absolute_import as _abs
import numpy as np
import tvm
import topi
from topi.util import get_const_tuple, get_const_int
from collections import namedtuple

from topi.nn.conv2d import _get_schedule
from topi.nn.conv2d import _get_workload
from topi.nn.pad import pad

AVX512ConvCommonFwd = namedtuple('AVX512ConvCommonFwd',
                                 ['ic_bn', 'oc_bn', 'reg_n', 'unroll_kw', 'layout_in', 'layout_out'])


def _declaration_conv(wkl, data, kernel):
    sch = _get_schedule(wkl)

    HPAD, WPAD = wkl.hpad, wkl.wpad
    HSTR, WSTR = wkl.hstride, wkl.wstride

    ndim_input = len(data.shape)

    if ndim_input == 5:
        batch_size, in_channel_chunk, in_height, in_width, in_channel_block = get_const_tuple(data.shape)
        in_channel = in_channel_block * in_channel_chunk
    else:
        assert ndim_input == 4
        in_channel_block = 0
        batch_size, in_channel, in_height, in_width = get_const_tuple(data.shape)

    num_filter, _, kernel_height, kernel_width, _, co = get_const_tuple(kernel.shape)
    num_filter *= co

    pad_height = in_height + 2 * HPAD
    pad_width = in_width + 2 * WPAD

    out_height = (in_height + 2 * HPAD - kernel_height) // HSTR + 1
    out_width = (in_width + 2 * WPAD - kernel_width) // WSTR + 1

    # pack data
    DOPAD = (HPAD != 0 and WPAD != 0)
    if DOPAD:
        if ndim_input == 5:
            data_pad = pad(data, (0, 0, HPAD, WPAD, 0), name="data_pad")
        else:
            assert ndim_input == 4
            data_pad = pad(data, (0, 0, HPAD, WPAD), name="data_pad")
    else:
        data_pad = data

    if in_channel_block != sch.ic_bn:
        print('WARNING!!! (common) in_channel_block=%d vs sch.ic_bn=%d' % (in_channel_block, sch.ic_bn))
        shape = (batch_size, in_channel // sch.ic_bn, pad_height, pad_width, sch.ic_bn)
        if ndim_input == 5:
            data_vec = tvm.compute(shape,
                                   lambda n, C, h, w, c:
                                   data_pad[n, (C * sch.ic_bn + c) // in_channel_block, h, w, (C * sch.ic_bn + c) % in_channel_block],
                                   name='data_vec', tag="conv2d_data_pack")
        else:
            assert ndim_input == 4
            data_vec = tvm.compute(shape,
                                   lambda n, C, h, w, c:
                                   data_pad[n, (C * sch.ic_bn + c), h, w],
                                   name='data_vec', tag="conv2d_data_pack")
            # data_pad = data_vec
    else:
        data_vec = data_pad

    kernel_vec = kernel

    # convolution
    oshape = (batch_size, num_filter//sch.oc_bn, out_height, out_width, sch.oc_bn)

    ic = tvm.reduce_axis((0, in_channel), name='ic')
    kh = tvm.reduce_axis((0, kernel_height), name='kh')
    kw = tvm.reduce_axis((0, kernel_width), name='kw')

    import re
    unpack_channel_block = re.findall(r'\d+', sch.layout_out)
    if len(unpack_channel_block) == 0:
        conv = tvm.compute(oshape, lambda n, oc_chunk, oh, ow, oc_block:
            tvm.sum(data_vec[n, ic // sch.ic_bn, oh * HSTR + kh, ow * WSTR + kw, ic % sch.ic_bn] *
                kernel_vec[oc_chunk, ic // sch.ic_bn, kh, kw, ic % sch.ic_bn, oc_block],
                axis=[ic, kh, kw]), name='conv2d')  # , tag="conv2d_nChwc")
        unpack_shape = (batch_size, num_filter, out_height, out_width)
        unpack = tvm.compute(unpack_shape,
                             lambda n, c, h, w: conv[n, c // sch.oc_bn, h, w, c % sch.oc_bn],
                             name='output_unpack',
                             tag='conv2d_nChwc_unpack')
    else:
        assert len(unpack_channel_block) == 1
        unpack_channel_block = int(unpack_channel_block[0])
        if unpack_channel_block == sch.oc_bn:
            return tvm.compute(oshape, lambda n, oc_chunk, oh, ow, oc_block:
                    tvm.sum(data_vec[n, ic // sch.ic_bn, oh * HSTR + kh, ow * WSTR + kw, ic % sch.ic_bn] *
                    kernel_vec[oc_chunk, ic // sch.ic_bn, kh, kw, ic % sch.ic_bn, oc_block],
                    axis=[ic, kh, kw]), name='conv2d', tag="conv2d_nChwc")
        else:
            conv = tvm.compute(oshape, lambda n, oc_chunk, oh, ow, oc_block:
            tvm.sum(data_vec[n, ic // sch.ic_bn, oh * HSTR + kh, ow * WSTR + kw, ic % sch.ic_bn] *
                    kernel_vec[oc_chunk, ic // sch.ic_bn, kh, kw, ic % sch.ic_bn, oc_block],
                    axis=[ic, kh, kw]), name='conv2d')
            unpack_shape = (batch_size, num_filter//unpack_channel_block, out_height, out_width, unpack_channel_block)
            unpack = tvm.compute(unpack_shape,
                                 lambda n, C, h, w, c: conv[n, (C * unpack_channel_block + c) // sch.oc_bn, h, w, (C * unpack_channel_block + c) % sch.oc_bn],
                                 name='output_unpack',
                                 tag='conv2d_nChwc_unpack')

    return unpack


def _schedule_conv(s, wkl, data, data_pad, data_vec, kernel, conv_out, output, last):
    sch = _get_schedule(wkl)

    HPAD, WPAD = wkl.hpad, wkl.wpad
    DOPAD = (HPAD != 0 and WPAD != 0)

    # A, W = data, kernel_vec
    A0, A1 = data_pad, data_vec

    # schedule data
    if DOPAD and "conv2d_data_pack" in s[A1].op.tag:
        s[A0].compute_inline()
    if isinstance(s[A1].op, tvm.tensor.ComputeOp): #and "conv2d_data_pack" in s[A1].op.tag:
        batch, ic_chunk, ih, iw, ic_block = s[A1].op.axis
        parallel_axis = s[A1].fuse(ic_chunk, ih)
        s[A1].parallel(parallel_axis)

    # schedule conv
    C, O0, O = conv_out, output, last
    CC = s.cache_write(C, 'global')

    _, oc_chunk, oh, ow, oc_block = s[C].op.axis
    ow_chunk, ow_block = s[C].split(ow, factor=sch.reg_n)
    s[C].reorder(oc_chunk, oh, ow_chunk, ow_block, oc_block)
    parallel_axis = s[C].fuse(oc_chunk, oh)
    s[C].vectorize(oc_block)
    if C == O:
        s[C].parallel(parallel_axis)

    s[CC].compute_at(s[C], ow_chunk)
    _, oc_chunk, oh, ow, oc_block = s[CC].op.axis
    ic, kh, kw = s[CC].op.reduce_axis

    ow_chunk, ow_block = s[CC].split(ow, factor=sch.reg_n)
    ic_chunk, ic_block = s[CC].split(ic, factor=sch.ic_bn)

    if sch.unroll_kw:
        s[CC].reorder(oc_chunk, oh, ow_chunk, ic_chunk, kh, ic_block, kw, ow_block, oc_block)
        s[CC].unroll(kw)
    else:
        s[CC].reorder(oc_chunk, oh, ow_chunk, ic_chunk, kh, kw, ic_block, ow_block, oc_block)

    s[CC].vectorize(oc_block)
    s[CC].unroll(ow_block)

    if O0 != O:
        s[O0].compute_inline()

    if C != O:
        if len(s[O].op.axis) == 5:
            batch, oc_chunk, oh, ow, oc_block = s[O].op.axis
            ow_chunk, ow_block = s[O].split(ow, factor=sch.reg_n)
            s[O].reorder(oc_chunk, oh, ow_chunk, ow_block, oc_block)
            parallel_axis = s[O].fuse(oc_chunk, oh)
            s[C].compute_at(s[O], parallel_axis)
            _, oc_block = s[O].split(oc_block, factor=sch.oc_bn)
            s[O].vectorize(oc_block)

            s[O].parallel(parallel_axis)
        else:
            assert len(s[O].op.axis) == 4
            batch, oc, oh, ow = s[O].op.axis
            ow_chunk, ow_block = s[O].split(ow, factor=sch.reg_n)
            oc_chunk, oc_block = s[O].split(oc, factor=sch.oc_bn)
            s[O].reorder(oc_chunk, oh, ow_chunk, ow_block, oc_block)
            parallel_axis = s[O].fuse(oc_chunk, oh)
            s[C].compute_at(s[O], parallel_axis)
            s[O].vectorize(oc_block)

            s[O].parallel(parallel_axis)

    return s
