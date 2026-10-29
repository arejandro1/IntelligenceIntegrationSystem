# -*- coding: utf-8 -*-
"""测试运行器：运行指定测试模块，结果同时输出到控制台和 _test_result.txt

- 无参数：glob 发现 Test/test_*.py 全部模块（按文件名排序），逐模块隔离运行；
- 带参数：仅运行命令行指定的模块（与旧版语义一致）；
- 逐模块独立加载与运行，单模块失败/跳过不影响后续模块；
- 输出契约不变：_test_result.txt 末尾四行为 SUCCESS=/TESTS_RUN=/FAILURES=/ERRORS=（汇总值）。
"""
import glob
import io
import os
import sys
import traceback
import unittest
from typing import List


def discover_default_modules() -> List[str]:
    """无参数时扫描 Test/test_*.py，按文件名排序转为模块名列表。"""
    modules: List[str] = []
    for path in sorted(glob.glob(os.path.join("Test", "test_*.py"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        modules.append(f"Test.{stem}")
    return modules


def _is_module_skip_exception(exc: BaseException) -> bool:
    """判断模块加载期异常是否表示整模块跳过。

    pytest.importorskip 抛出的 Skipped 继承 BaseException 而非 Exception，
    unittest.SkipTest 继承 Exception，两类都需识别为跳过而非加载错误。
    """
    return type(exc).__name__ == "Skipped" or isinstance(exc, unittest.SkipTest)


def run(modules: List[str]) -> int:
    # 捕获输出到字符串（所有模块的运行输出写入同一缓冲）
    buf = io.StringIO()
    loader = unittest.TestLoader()

    total_run = 0
    total_failures = 0
    total_errors = 0
    total_skipped = 0
    module_lines: List[str] = []
    all_ok = True

    for module_name in modules:
        try:
            suite = loader.loadTestsFromName(module_name)
        except BaseException as exc:  # 需捕获 BaseException：pytest 的 Skipped 不继承 Exception
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if _is_module_skip_exception(exc):
                # 整模块跳过（如 test_vectordb_fixes 缺 chromadb）：不计失败，不中断后续模块
                buf.write(f"MODULE_SKIPPED {module_name}: {exc}\n\n")
                module_lines.append(f"{module_name}: skipped(整模块跳过)")
                continue
            # 其余加载异常：记为该模块加载错误，计入 ERRORS 汇总并打印 traceback
            buf.write(f"MODULE_LOAD_ERROR {module_name}: {type(exc).__name__}: {exc}\n")
            buf.write(traceback.format_exc())
            total_errors += 1
            all_ok = False
            module_lines.append(f"{module_name}: run=0 fail=0 err=1(load) skip=0")
            continue

        # 单模块独立运行：失败不影响后续模块
        runner = unittest.TextTestRunner(stream=buf, verbosity=2)
        result = runner.run(suite)
        run_n = result.testsRun
        fail_n = len(result.failures)
        err_n = len(result.errors)
        skip_n = len(getattr(result, "skipped", []))
        total_run += run_n
        total_failures += fail_n
        total_errors += err_n
        total_skipped += skip_n
        if not result.wasSuccessful():
            all_ok = False
        module_lines.append(
            f"{module_name}: run={run_n} fail={fail_n} err={err_n} skip={skip_n}"
        )

    # 末尾按模块打印一行汇总，再打印总计行
    summary = ("\n===== 模块汇总 =====\n" + "\n".join(module_lines) + "\n"
               + f"TOTAL: run={total_run} fail={total_failures} err={total_errors} skip={total_skipped}\n")

    output = buf.getvalue() + summary
    success = all_ok
    # 写入文件（保持既有输出契约：末尾四行为汇总值）
    with open('_test_result.txt', 'w', encoding='utf-8') as f:
        f.write(output)
        f.write(f"\nSUCCESS={success}\n")
        f.write(f"TESTS_RUN={total_run}\n")
        f.write(f"FAILURES={total_failures}\n")
        f.write(f"ERRORS={total_errors}\n")
    # 同时打印到控制台
    print(output)
    print(f"SUCCESS={success}")
    return 0 if success else 1


if __name__ == '__main__':
    mods = sys.argv[1:] if len(sys.argv) > 1 else discover_default_modules()
    sys.exit(run(mods))