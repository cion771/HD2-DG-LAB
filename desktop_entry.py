"""PyInstaller entry point; never include a user's config or runtime logs."""
from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    from hd2coyote.desktop import main
    raise SystemExit(main())
