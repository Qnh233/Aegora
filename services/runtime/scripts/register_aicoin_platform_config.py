from __future__ import annotations

import json

from aegora_runtime.aicoin_platform_seed import register_aicoin_platform_config, result_payload


def main() -> None:
    result = register_aicoin_platform_config()
    print(json.dumps(result_payload(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
