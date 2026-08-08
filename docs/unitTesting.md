# Unit Testing

NVDA Clipboard uses Python's standard `unittest` framework for its standalone tests.

## Running Tests Locally

To run the unit test suite locally using `uv`:

``` bash
uv run python -m unittest discover -s tests -v
```

Or execute a specific test module:

``` bash
uv run python -m unittest -v tests.test_storage
```

The test suite runs on Windows because the add-on loads its bundled SQLite extension.

## Type Checking

Pyright is included in the development environment to follow the NVDA add-on template.
The current NVDA source APIs are dynamically typed and are not shipped with complete
stubs, so the Pyright Prek hook is skipped by the build workflow while the type debt is
reduced incrementally.
