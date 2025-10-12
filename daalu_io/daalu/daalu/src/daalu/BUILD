load("@aspect_rules_py//py:defs.bzl", "py_library")

# gazelle:resolve py daalu //src/daalu:daalu
# gazelle:resolve py daalu.config //src/daalu:daalu
# gazelle:resolve py daalu.config.loader //src/daalu:daalu
# gazelle:resolve py daalu.helm //src/daalu:daalu

py_library(
    name = "daalu",
    srcs = glob(["**/*.py"]),        # automatically include all Python files
    imports = [
        "..",
        "src",           # allows Bazel to add /src to PYTHONPATH
        "src/daalu",
        "src/daalu/helm",    # explicitly add /src/daalu to PYTHONPATH
    ],               # make 'from daalu.config.loader' work
    visibility = ["//visibility:public"],  # expose to all packages
    deps = [
        "@pip//pydantic",
        "@pip//pyyaml",
        "@pip//paramiko",
        "@pip//jinja2",
    ],
)

load("@aspect_rules_py//py:defs.bzl", "py_binary")

py_binary(
    name = "cli_app",
    srcs = ["cli/app.py"],
    deps = [
        ":daalu",           # main library in src/daalu
        "@pip//typer",
        "@pip//pydantic",
        "@pip//pyyaml",
        "@pip//jinja2",
    ],
    data = ["//:cluster-defs"]
)