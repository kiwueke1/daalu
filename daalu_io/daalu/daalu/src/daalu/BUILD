# src/daalu/BUILD

load("@aspect_rules_py//py:defs.bzl", "py_library")

# gazelle:resolve py daalu //src/daalu:daalu
# gazelle:resolve py daalu.config //src/daalu:daalu
# gazelle:resolve py daalu.config.loader //src/daalu:daalu
# gazelle:resolve py daalu.helm //src/daalu:daalu

py_library(
    name = "daalu",
    #srcs = glob(["**/*.py"]),        # automatically include all Python files
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "temporal/**",
            #"cli/**",
        ],
    ),    
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
        "@pip//pymysql:pkg",
    ],
)

load("@aspect_rules_py//py:defs.bzl", "py_binary")

py_binary(
    name = "cli_app",
    srcs = [
        "cli/app.py",
    ],
    deps = [
        ":daalu",   # this is the full daalu package
        "@pip//typer:pkg",
        "@pip//pydantic:pkg",
        "@pip//pyyaml:pkg",
        "@pip//jinja2:pkg",
        "//src/daalu/temporal:temporal",
    ],
)

