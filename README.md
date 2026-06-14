# Eventyay Hubspot Integration Plugin

This is a plugin for [Eventyay](https://github.com/fossasia/eventyay) that enables seamless integration with Hubspot.

## Development Setup

1. Set up a working [Eventyay Development Setup](https://github.com/fossasia/eventyay?tab=readme-ov-file#getting-started).
2. Clone this repository:
   ```bash
   git clone https://github.com/fossasia/eventyay-hubspot.git
   cd eventyay-hubspot
   ```
3. Activate the [virtual environment](https://github.com/fossasia/eventyay?tab=readme-ov-file#getting-started) you use for Eventyay development.
4. Install the plugin in editable mode to register it with the Eventyay plugin registry:
   ```bash
   uv pip install -e .
   ```
5. Compile the translations:
   ```bash
   make
   ```
6. Restart your local Eventyay server. You can now enable and configure the Hubspot plugin from the **Plugins** tab in your event settings.

## Code Style & Linting

This repository enforces code style guidelines via CI. You can run checks locally by installing the development dependencies:

```bash
pip install pre-commit ruff black
pre-commit install

pre-commit run --all-files
```

## License

Copyright 2024 FOSSASIA

Released under the terms of the [Apache License 2.0](LICENSE).
