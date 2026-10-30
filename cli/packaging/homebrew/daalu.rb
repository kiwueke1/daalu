# Homebrew formula for the Daalu CLI.
#
# Lives in the tap repo daalu-io/homebrew-tap as Formula/daalu.rb. CI rewrites
# the version + url + sha256 on each release. Install with:
#
#   brew install daalu-io/tap/daalu
#
# The {{ }} tokens are filled in by the release workflow.
class Daalu < Formula
  desc "Daalu coding agent for your terminal"
  homepage "https://daalu.io"
  version "{{VERSION}}"

  on_macos do
    on_arm do
      url "https://github.com/daalu-io/cli/releases/download/cli-v{{VERSION}}/daalu-macos-arm64.tar.gz"
      sha256 "{{SHA256_MACOS_ARM64}}"
    end
    on_intel do
      url "https://github.com/daalu-io/cli/releases/download/cli-v{{VERSION}}/daalu-macos-x64.tar.gz"
      sha256 "{{SHA256_MACOS_X64}}"
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/daalu-io/cli/releases/download/cli-v{{VERSION}}/daalu-linux-arm64.tar.gz"
      sha256 "{{SHA256_LINUX_ARM64}}"
    end
    on_intel do
      url "https://github.com/daalu-io/cli/releases/download/cli-v{{VERSION}}/daalu-linux-x64.tar.gz"
      sha256 "{{SHA256_LINUX_X64}}"
    end
  end

  def install
    bin.install "daalu"
  end

  test do
    assert_match "daalu #{version}", shell_output("#{bin}/daalu --version")
  end
end
