from benchmark.cli import main

if __name__ == "__main__":
    # 仅在模块入口执行 CLI；普通导入必须保持无副作用。
    raise SystemExit(main())
