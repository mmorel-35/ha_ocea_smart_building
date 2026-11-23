# OCEA client

This unpublished Python client provides asynchronous access to the OCEA
Smart Building resident API. It is embedded in the Home Assistant integration
and can also be installed locally without Home Assistant.

The caller supplies and closes the `aiohttp.ClientSession`. The source and
standalone tests are maintained in the repository's uv workspace.
