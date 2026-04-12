import config
import db


def main():
    settings = config.load()
    connection = db.connect(settings)
    print(f"Connected: {connection}")


if __name__ == "__main__":
    main()
