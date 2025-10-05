"""Definition of linter tools

This is in its own package because it has so many loading-time symbols,
we don't want to trigger eager fetches of these for builds which aren't running linters.
"""

package(default_visibility = ["//:__subpackages__"])








load("@rules_go//go:def.bzl", "nogo", "TOOLS_NOGO")

nogo(
    name = "nogo",
    deps = TOOLS_NOGO,
    visibility = ["//visibility:public"],
)

