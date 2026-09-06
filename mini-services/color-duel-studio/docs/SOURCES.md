# Technical references checked for this build

Checked 2026-09-06. These document API/algorithm capabilities, not the quality of any generated art.

- OpenAI image generation and editing: https://developers.openai.com/api/docs/guides/image-generation
- OpenAI structured output: https://developers.openai.com/api/docs/guides/structured-outputs
- Configurable image model: https://developers.openai.com/api/docs/models/gpt-image-2
- Configurable brief/chat model: https://developers.openai.com/api/docs/models/gpt-5.4-mini
- SLIC segmentation API and approximate region-count behavior: https://scikit-image.org/docs/stable/api/skimage.segmentation.html#skimage.segmentation.slic
- Rasterio polygonization and rasterization: https://rasterio.readthedocs.io/en/stable/topics/features.html

The optional provider uses the Responses endpoint for structured conversational art briefs and the Images generation/edit endpoints for explicitly requested master images. Live provider calls were not executed in this environment. Contract tests use a mock HTTP transport; provider responses, model access, latency and billing still need validation with the user's account.
