from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tandem-cli")
except PackageNotFoundError:  # source tree without an installed dist
    __version__ = "0.0.0.dev0"
