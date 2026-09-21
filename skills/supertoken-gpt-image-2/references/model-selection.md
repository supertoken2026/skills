# GPT Image 2 / 2.5 模型选择

核对日期：2026-09-21。主要支持 `gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`。三个 ID 已在 SuperToken 的实时 `GET /v1/models` 中确认。Skill 名称继续使用 `supertoken-gpt-image-2`，原安装和升级命令保持有效。

## 选择依据

OpenAI 将 Flare 定位为速度优先的日常高质量生图模型，将 Sunburst 定位为能力更强、注重编辑精度的模型。结合这一定位，本 Skill 建议：

- Flare：日常文章配图、概念草稿、需要反复比较的视觉方案。没有明确偏好时默认使用。
- Sunburst：产品主图、细密材质、需要保留主体细节的局部修改，以及以最终画质为优先的交付。预留更长等待时间。
- GPT Image 2：用户明确要求 2.0、复现已有工作流或与旧结果对照时使用；不会因为新增 2.5 而删除。

这些场景是选型建议，不保证某个模型在每张图上都更快或更好。模型与 `quality` 分开选择：CLI 默认 `low`，成品可显式用 `high`。用户已指定模型或参数时按其要求执行。

## 渠道与能力边界

- 先以当前 Token 的 `models` 输出确认权限。模型可见只代表可请求的名称，不代表全部端点和参数已经验证。
- 通过 `POST /v1/images/generations` 同步生图；`--model` 原样传递完整 ID，不把 2.5 改写成旧版或 Adobe 别名。
- 上游两个 2.5 模型都支持图片输入和编辑。SuperToken 编辑、多图和异步功能仍受具体渠道约束；测试结果见[验证记录](https://github.com/supertoken2026/skills/blob/main/docs/validation/2026-09-21-image-2.5.md)。
- 上游支持 `low`、`medium`、`high`、`xhigh`、`max`、`auto`。这些不等于所有 SuperToken 渠道都支持；先使用已验证参数，额外档位按渠道文档与实际响应确认。
- 上游计费、速度百分比和参数范围不直接当作 SuperToken 的价格、时延承诺或完整兼容性结论。
- `gpt-image-2-count`、Adobe `-count` 名称保留兼容入口，仅在用户指定且实时列表存在时调用。它们不计入上面的三个主要型号。
- 生成与编辑 POST 不自动重试。请求失败或超时后先确认结果，避免重复提交和重复计费。

## 资料来源

使用 AnySearch 检索并读取以下模型页：

- [OpenAI：GPT Image 2.5 Flare](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)：日常生图、速度定位与质量档位。
- [OpenAI：GPT Image 2.5 Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst)：编辑精度定位与质量档位。
- [SuperToken 图片 API](https://docs.supertoken.cc/api/image/) 与 [GPT Image 概览](https://docs.supertoken.cc/api/gpt-image-2-new)：端点、鉴权及“模型以实际列表为准”的约定。核对时静态页面尚未列出 2.5，因此模型 ID 使用实时查询结果。

## English

Use Flare by default for everyday images and quick iteration. Choose Sunburst for precision edits, detailed product artwork, or final assets where a longer wait is acceptable. Retain GPT Image 2 for explicit 2.0 requests and existing workflows. These are routing recommendations based on the model pages above, not a guarantee for every image.

Keep the exact model ID and honor user choices. Check account access with `models`. Upstream editing, quality levels, pricing, and speed claims do not establish SuperToken channel compatibility or pricing. Model and quality are independent; the CLI defaults to `low`, with `high` available explicitly for final work. Do not automatically switch models or retry a creation POST after failure.
