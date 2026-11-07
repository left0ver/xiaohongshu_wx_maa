# PP-OCRv5 mobile OCR models

Updated: 2026-11-07.

Both plugins use the official PaddleOCR ONNX mobile models. MaaFramework loads
`det.onnx`, `rec.onnx`, and `keys.txt` from this directory. Keep the recognition
model and its matching dictionary together when updating these assets.

| File | Model / source | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `det.onnx` | `PP-OCRv5_mobile_det` | 4826518 | `a431985659dc921974177a95adcfbb90fd9e51989a5e04d70d0b75f597b6e61d` |
| `rec.onnx` | `PP-OCRv5_mobile_rec` | 16534782 | `da72dc72ca4dc220df0dfde68c1dedc31c58d3e76a25871122e5056227d50092` |
| `keys.txt` | `ppocrv5_dict.txt` | 74012 | `d1979e9f794c464c0d2e0b70a7fe14dd978e9dc644c0e71f14158cdf8342af1b` |

## Sources

- [Official Android deployment instructions](https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/cross_platform/android_deployment.html)
- [Detection ONNX archive](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_det_onnx_infer.tar)
- [Recognition ONNX archive](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_rec_onnx_infer.tar)
- [Recognition dictionary](https://github.com/PaddlePaddle/PaddleOCR/blob/main/ppocr/utils/dict/ppocrv5_dict.txt)

The ONNX files are copied from each archive's `inference.onnx`. Their SHA-256
hashes match the Hugging Face download metadata included in the official archives.
The recognition model supports simplified Chinese, traditional Chinese, English,
and Japanese. Model weights and dictionary are distributed under Apache-2.0;
see `LICENSE-PaddleOCR`. `LICENSE-MaaCommonAssets` is retained for provenance of
the previous assets.

## Validation

A phone-side MaaFramework comparison on Hebe-BD00 / Android 11 (2026-11-07)
loaded both model pairs with the CPU backend and matching dictionaries. It used
the same saved 720 x 1559 comment screenshot, alternating model order each round.

| Warm call (median) | PP-OCRv4 | PP-OCRv5 mobile | Runs per model |
| --- | ---: | ---: | ---: |
| Full image detection + recognition | 2.760 s | 3.007 s | 5 |
| 240 x 779 region detection + recognition | 0.888 s | 1.016 s | 5 |
| Same 192 x 31 single line, recognition only | 53.4 ms | 60.5 ms | 10 |

Full-image means were 3.117 s and 3.057 s respectively; timing varied, so this
small sample does not establish a stable speed improvement. Measurements exclude
screenshot capture and ADB transport. Both models correctly read the single-line
sample. This validates basic runtime compatibility, not overall accuracy or a
complete app workflow. Local evidence (not tracked in Git):
`plugins/wechat/data/acceptance/ocr-compare-v4-v5-20261107/result.json`.
