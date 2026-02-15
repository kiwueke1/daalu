from jinja2 import Environment, FileSystemLoader
from pathlib import Path
import os
import re

def expand_env_vars(value: str) -> str:
    return re.sub(r"\$\{([^}^{]+)\}", lambda m: os.getenv(m.group(1), m.group(0)), value)

class TemplateRenderer:
    def __init__(self, templates_dir: Path):
        self.env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)

    def render(self, template_name: str, context: dict) -> str:
        expanded = {k: expand_env_vars(str(v)) if isinstance(v, str) else v for k, v in context.items()}
        tmpl = self.env.get_template(template_name)
        return tmpl.render(**expanded)
