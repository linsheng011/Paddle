/* Copyright (c) 2016 PaddlePaddle Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License. */

#pragma once

#include "paddle/fluid/framework/data_type.h"
#include "paddle/fluid/framework/op_registry.h"
#include "paddle/fluid/framework/phi_utils.h"
#include "paddle/fluid/platform/transform.h"
#include "paddle/phi/kernels/cast_kernel.h"

namespace paddle {
namespace operators {

template <typename InT, typename OutT>
struct CastOpTransformFunctor {
  HOSTDEVICE OutT operator()(InT in) const { return static_cast<OutT>(in); }
};

template <typename DeviceContext, typename InT>
struct CastOpFunctor {
  const phi::DenseTensor* in_;
  phi::DenseTensor* out_;
  const DeviceContext& ctx_;
  CastOpFunctor(const phi::DenseTensor* in,
                phi::DenseTensor* out,
                const DeviceContext& ctx)
      : in_(in), out_(out), ctx_(ctx) {}

  template <typename OutT>
  void apply() const {
    auto* in_begin = in_->data<InT>();
    auto numel = in_->numel();
    auto* in_end = in_begin + numel;
    auto* out_begin = out_->mutable_data<OutT>(ctx_.GetPlace());
    platform::Transform<DeviceContext> trans;
    trans(
        ctx_, in_begin, in_end, out_begin, CastOpTransformFunctor<InT, OutT>());
  }
};

template <typename DeviceContext, typename InT>
class CastOpKernel : public framework::OpKernel<InT> {
 public:
  void Compute(const framework::ExecutionContext& context) const override {
    auto* in = context.Input<phi::DenseTensor>("X");
    auto* out = context.Output<phi::DenseTensor>("Out");

    auto out_dtype = context.Attr<int>("out_dtype");

    auto& dev_ctx = context.device_context<DeviceContext>();
    out->mutable_data(dev_ctx.GetPlace(),
                      static_cast<framework::proto::VarType::Type>(out_dtype));

    auto pt_out_dtype = framework::TransToPhiDataType(
        static_cast<framework::proto::VarType::Type>(out_dtype));

    // call new kernel
    phi::CastKernel<InT>(
        static_cast<const typename paddle::framework::ConvertToPhiContext<
            DeviceContext>::TYPE&>(dev_ctx),
        *in,
        pt_out_dtype,
        out);
  }
};

}  // namespace operators
}  // namespace paddle
