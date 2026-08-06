from __future__ import annotations

import nonebot
from nonebot.adapters.onebot.v11 import Adapter


def main() -> None:
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    nonebot.load_plugin("luna_kb.nonebot_plugin")
    nonebot.run()


if __name__ == "__main__":
    main()
