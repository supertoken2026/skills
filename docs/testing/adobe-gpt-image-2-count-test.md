# Adobe GPT Image 2 Count 渠道测试

这是一份给渠道方使用的最小 API 测试说明。请求使用 SuperToken 统一图片端点，但模型指定为 `adobe-gpt-image-2-count`。

> 本文中的 key 是临时测试 key，按测试用途暴露。正式环境请改用环境变量或密钥管理服务，并在测试结束后撤销或轮换此 key。

## 测试参数

| 参数 | 值 |
| --- | --- |
| Base URL | `https://api.supertoken.cc` |
| Endpoint | `POST /v1/images/generations` |
| Model | `adobe-gpt-image-2-count` |
| 测试 key | `sk-BLsQtPhxd6S2vePZMeWfmU5KUVEs2DKZQ1Xg1w7CKHeEnd5w` |
| Size | `2048x2048` |
| Quality | `high` |
| Output format | `png` |
| Count | `n=1` |
| 建议客户端超时 | `600` 秒 |

这个渠道生成较慢。客户端超时不要使用默认的 180 秒，建议设置为 600 秒；最低应大于 180 秒，建议不低于 300 秒。连接超时可以单独设置为 30 秒。

## 请求体

```json
{
  "model": "adobe-gpt-image-2-count",
  "prompt": "Use case: cinematic concept art. Asset type: high-resolution key art. Create an original cinematic science-fiction city at blue hour after a light rain. Composition: a wide symmetrical street canyon viewed from a low eye-level camera, with a clear central vanishing point, layered depth from reflective foreground pavement to midground pedestrians and elevated transit rails, and distant towers fading into atmospheric haze. Architecture: elegant near-future skyscrapers with believable structural details, glass and brushed metal facades, warm window grids, subtle illuminated wayfinding panels with abstract symbols only, no readable words, no logos, no brands, and no recognizable real-world landmarks. Foreground: crisp puddle reflections, a few transparent umbrellas, small autonomous delivery vehicles, tactile wet asphalt, scattered amber and cyan practical lights. Atmosphere: soft mist, restrained volumetric light, rain droplets catching light, realistic scale, calm hopeful mood rather than dystopian. Color palette: deep teal and indigo shadows balanced by amber, coral, and cool white highlights. Lighting: physically plausible cinematic lighting, soft overcast sky glow, selective neon reflections, gentle rim light on silhouettes. Style: premium photorealistic production design blended with refined concept-art polish, highly detailed materials, natural perspective, coherent geometry, clean edges, no excessive bloom, no clutter. Output constraints: no text, no watermark, no captions, no signatures, no extra limbs, no distorted faces, no warped buildings.",
  "n": 1,
  "size": "2048x2048",
  "quality": "high",
  "output_format": "png"
}
```

注意：参数名是 `output_format`，不是 `response_format`。

## curl 调用

```bash
API_KEY='sk-BLsQtPhxd6S2vePZMeWfmU5KUVEs2DKZQ1Xg1w7CKHeEnd5w'

curl --fail-with-body --location \
  --connect-timeout 30 \
  --max-time 600 \
  'https://api.supertoken.cc/v1/images/generations' \
  -H "Authorization: Bearer ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  --data @- <<'JSON'
{
  "model": "adobe-gpt-image-2-count",
  "prompt": "A cinematic science-fiction city at blue hour after a light rain, wide symmetrical street canyon, reflective wet pavement, near-future glass and brushed-metal towers, elevated transit rails, transparent umbrellas, autonomous delivery vehicles, deep teal and indigo shadows balanced by amber and cool-white lights, soft mist, realistic materials and perspective, no readable text, no logos, no watermark.",
  "n": 1,
  "size": "2048x2048",
  "quality": "high",
  "output_format": "png"
}
JSON
```

成功响应中的图片地址位于 `data[0].url`。该地址可能是临时签名 URL，应立即下载，不要长期保存为永久地址：

```bash
curl --fail --location --max-time 120 \
  -o ./adobe-gpt-image-2-count-result.png \
  '<data[0].url>'
```

## Node.js 调用

```js
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(), 600_000);

try {
  const response = await fetch(
    "https://api.supertoken.cc/v1/images/generations",
    {
      method: "POST",
      headers: {
        Authorization: "Bearer sk-BLsQtPhxd6S2vePZMeWfmU5KUVEs2DKZQ1Xg1w7CKHeEnd5w",
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      signal: controller.signal,
      body: JSON.stringify({
        model: "adobe-gpt-image-2-count",
        prompt: "A cinematic science-fiction city at blue hour after a light rain, wide symmetrical street canyon, reflective wet pavement, near-future glass and brushed-metal towers, elevated transit rails, transparent umbrellas, autonomous delivery vehicles, deep teal and indigo shadows balanced by amber and cool-white lights, soft mist, realistic materials and perspective, no readable text, no logos, no watermark.",
        n: 1,
        size: "2048x2048",
        quality: "high",
        output_format: "png",
      }),
    },
  );

  const payload = await response.json();
  if (!response.ok) {
    throw new Error(JSON.stringify(payload));
  }

  console.log(payload.data?.[0]?.url);
} finally {
  clearTimeout(timeout);
}
```

## 返回值与错误排查

成功响应应包含：

```json
{
  "data": [
    {
      "url": "https://..."
    }
  ]
}
```

常见情况：

| 返回 | 含义 |
| --- | --- |
| `200` 且有 `data[0].url` | 生成成功，可以下载图片 |
| `401` | key 无效、过期或 Authorization 格式错误 |
| `503 model_not_found` | 当前模型没有可用渠道，不是提示词错误 |
| 客户端超时 | 先确认超时是否至少 300-600 秒；不要立即判定渠道失败 |

本渠道一次实际测试在默认 180 秒内超时，将客户端超时提高到 300 秒后生成成功；因此建议直接使用 `600` 秒。

## Python 调用

```python
import requests

response = requests.post(
    "https://api.supertoken.cc/v1/images/generations",
    headers={
        "Authorization": "Bearer sk-BLsQtPhxd6S2vePZMeWfmU5KUVEs2DKZQ1Xg1w7CKHeEnd5w",
        "Content-Type": "application/json",
        "Accept": "application/json",
    },
    json={
        "model": "adobe-gpt-image-2-count",
        "prompt": "A cinematic science-fiction city at blue hour after a light rain, wide symmetrical street canyon, reflective wet pavement, near-future glass and brushed-metal towers, elevated transit rails, transparent umbrellas, autonomous delivery vehicles, deep teal and indigo shadows balanced by amber and cool-white lights, soft mist, realistic materials and perspective, no readable text, no logos, no watermark.",
        "n": 1,
        "size": "2048x2048",
        "quality": "high",
        "output_format": "png",
    },
    timeout=(30, 600),
)
response.raise_for_status()
print(response.json()["data"][0]["url"])
```
