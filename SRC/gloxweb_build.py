import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QMainWindow,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "GloxWeb"
APP_VERSION = "3.2"


def get_app_dir():
    """Keep projects and .env beside the script or packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_app_dir()
PROJECTS_DIR = BASE_DIR / "GloxWeb_Projects"
ENV_FILE = BASE_DIR / ".env"

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(ENV_FILE, override=True)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

SITE_URL = os.getenv(
    "GLOXWEB_SITE_URL",
    "https://glox-industries.ai.studio",
).strip()

SITE_TITLE = os.getenv("GLOXWEB_SITE_TITLE", "GloxWeb").strip()

REQUIRED_FILES = {
    "package.json",
    "index.html",
    "vite.config.ts",
    "tsconfig.json",
    "src/main.tsx",
    "src/App.tsx",
}

MAX_FILES = 80
MAX_FILE_CHARS = 500_000


# ============================================================
# PROJECT UTILITIES
# ============================================================

def sanitize_project_name(name):
    name = re.sub(r'[<>:"/\\|?*]', "_", str(name or "").strip())
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip(" ._")
    return (name or "GloxWeb_Project")[:100]


def create_project_folder(project_name):
    safe_name = sanitize_project_name(project_name)
    folder = PROJECTS_DIR / safe_name

    try:
        folder.mkdir(parents=True, exist_ok=False)
        return folder
    except FileExistsError:
        number = 2

        while True:
            candidate = PROJECTS_DIR / f"{safe_name}_{number}"

            try:
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            except FileExistsError:
                number += 1


def extract_json(text):
    """Accept JSON returned directly or surrounded by Markdown fences."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError as error:
                raise ValueError(
                    "The model returned malformed JSON."
                ) from error

        raise ValueError(
            "The model did not return a valid JSON project bundle."
        )


def validate_project_bundle(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("The project bundle must be a JSON object.")

    if not isinstance(bundle.get("files"), dict):
        raise ValueError(
            'Expected a JSON object containing a "files" object.'
        )

    files = bundle["files"]

    if not files:
        raise ValueError("The model returned an empty project.")

    if len(files) > MAX_FILES:
        raise ValueError(
            f"The model returned too many files ({len(files)}). "
            f"Limit: {MAX_FILES}."
        )

    cleaned = {}

    for raw_path, content in files.items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise ValueError(
                "Every file must have a string path and string content."
            )

        path_text = raw_path.replace("\\", "/").strip()
        path = Path(path_text)
        parts = path.parts

        if (
            not path_text
            or path.is_absolute()
            or ".." in parts
            or path_text.startswith("/")
            or ":" in path_text
            or not parts
            or parts[0] in {"node_modules", ".git", ".env"}
        ):
            raise ValueError(
                f"Unsafe or unsupported generated path: {raw_path}"
            )

        if len(content) > MAX_FILE_CHARS:
            raise ValueError(
                f"Generated file is too large: {raw_path}"
            )

        cleaned[path_text] = content

    missing = REQUIRED_FILES - set(cleaned)

    if missing:
        raise ValueError(
            "Project is missing required files: "
            + ", ".join(sorted(missing))
        )

    try:
        package = json.loads(cleaned["package.json"])
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Generated package.json is invalid JSON: {error}"
        ) from error

    if not isinstance(package, dict):
        raise ValueError("package.json must contain a JSON object.")

    scripts = package.get("scripts", {})

    if (
        not isinstance(scripts, dict)
        or "dev" not in scripts
        or "build" not in scripts
    ):
        raise ValueError(
            'package.json must define both "dev" and "build" scripts.'
        )

    return cleaned


# ============================================================
# AI GENERATION PROMPTS
# ============================================================

SYSTEM_PROMPT = r"""
You are GloxWeb, an autonomous senior frontend engineer.
Generate complete, coherent, runnable frontend projects.

OUTPUT CONTRACT:
Return ONLY one valid JSON object with this exact shape:

{"files":{"relative/path.ext":"complete file contents as a JSON string"}}

No Markdown fences, commentary, or extra keys.
JSON escaping must be valid.
Do not omit file contents or use placeholders.

DEFAULT STACK:
- Vite + React + TypeScript
- Modern semantic HTML and accessible React components
- CSS in separate .css files
- npm scripts: dev, build, preview
- package.json, index.html, vite.config.ts, tsconfig.json
- src/main.tsx, src/App.tsx, src/index.css
- Additional components, assets and utilities as required
- Use compatible, stable package versions
- Avoid unnecessary dependencies
- Do not use Next.js, a backend, environment secrets, or another
  build tool unless the user explicitly requests it

QUALITY REQUIREMENTS:
- Include every file required to run and build the project
- No TODOs, lorem ipsum, empty sections, fake controls or dead links
- Buttons and forms must do something meaningful
- Navigation must point to real sections or routes
- Responsive mobile, tablet and desktop layouts
- Keyboard accessibility and visible focus states
- Sensible contrast and accessible form labels
- Reduced-motion support
- Strong visual hierarchy, polished typography and intentional spacing
- Do not invent real-world statistics, testimonials, certifications,
  or business claims
- Keep source code understandable and split substantial UI into
  reusable components
- Never include API keys or secrets
- If requested functionality needs a backend or API key, build a safe
  frontend UI and use environment-variable placeholders when necessary
- Verify imports, exports, filenames, scripts, TypeScript types and JSON
  escaping before returning

Always include at minimum:
package.json, index.html, vite.config.ts, tsconfig.json,
src/main.tsx, src/App.tsx and src/index.css.
""".strip()


def build_prompt(project_name, architecture, theme, features, extra):
    return f"""
Create a complete, runnable Vite + React + TypeScript website project.

PROJECT NAME:
{project_name}

WEBSITE STRUCTURE / ARCHITECTURE:
{architecture}

THEME AND DESIGN DIRECTION:
{theme}

FEATURES AND INTERACTIONS:
{features}

EXTRA INSTRUCTIONS:
{extra or "No additional instructions."}

Return the entire project as a single JSON object shaped like:
{{"files": {{"relative/path": "full file content"}}}}

Use real relative file paths and include every required source and
configuration file. Escape all file contents correctly for JSON.

Do not return a single HTML-only website.
This must be a Vite + React + TypeScript project.
""".strip()


# ============================================================
# OPENROUTER MODEL LOADER
# ============================================================

class ModelLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def run(self):
        try:
            response = requests.get(
                OPENROUTER_MODELS_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}"
                },
                timeout=20,
            )

            response.raise_for_status()
            models_data = response.json().get("data", [])
            result = []

            for model in models_data:
                model_id = str(model.get("id", "")).strip()
                pricing = model.get("pricing") or {}

                try:
                    free = model_id.endswith(":free") or (
                        float(pricing.get("prompt")) == 0
                        and float(pricing.get("completion")) == 0
                    )
                except (TypeError, ValueError):
                    free = model_id.endswith(":free")

                if model_id and free:
                    result.append({
                        "id": model_id,
                        "name": str(model.get("name") or model_id),
                        "context_length": model.get("context_length"),
                    })

            result.sort(key=lambda item: item["name"].lower())
            self.loaded.emit(result)

        except Exception as error:
            self.failed.emit(str(error))


# ============================================================
# OPENROUTER CHAT CLIENT
# ============================================================

class OpenRouterClient:
    def __init__(self, api_key):
        self.api_key = api_key

    def generate(
        self,
        model,
        system_prompt,
        user_prompt,
        token_callback=None,
    ):
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 30000,
            "stream": True,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL,
            "X-Title": SITE_TITLE,
        }

        with requests.post(
            OPENROUTER_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=(20, 600),
            stream=True,
        ) as response:

            if response.status_code >= 400:
                try:
                    data = response.json()
                    message = data.get("error", {}).get(
                        "message", response.text
                    )
                except Exception:
                    message = response.text

                raise RuntimeError(
                    f"OpenRouter HTTP {response.status_code}: {message}"
                )

            output = []

            for raw_line in response.iter_lines(
                decode_unicode=True
            ):
                if not raw_line:
                    continue

                line = raw_line.strip()

                if not line.startswith("data:"):
                    continue

                data_text = line[5:].strip()

                if data_text == "[DONE]":
                    break

                try:
                    data = json.loads(data_text)
                except json.JSONDecodeError:
                    continue

                choices = data.get("choices", [])

                if not choices:
                    continue

                content = choices[0].get(
                    "delta", {}
                ).get("content", "")

                if isinstance(content, str) and content:
                    output.append(content)

                    if token_callback:
                        token_callback(content)

            return "".join(output)


# ============================================================
# PROJECT GENERATION WORKER
# ============================================================

class GenerationWorker(QObject):
    token = Signal(str)
    status = Signal(str)
    progress = Signal(int)
    completed = Signal(str, str, int)
    failed = Signal(str)

    def __init__(
        self,
        project_name,
        model,
        architecture,
        theme,
        features,
        extra,
    ):
        super().__init__()
        self.project_name = project_name
        self.model = model
        self.architecture = architecture
        self.theme = theme
        self.features = features
        self.extra = extra

    def run(self):
        try:
            self.status.emit("Preparing project prompt...")
            self.progress.emit(5)

            client = OpenRouterClient(OPENROUTER_API_KEY)

            prompt = build_prompt(
                self.project_name,
                self.architecture,
                self.theme,
                self.features,
                self.extra,
            )

            self.status.emit(
                f"Generating multi-file project with {self.model}..."
            )
            self.progress.emit(10)

            raw = client.generate(
                self.model,
                SYSTEM_PROMPT,
                prompt,
                self.token.emit,
            )

            self.progress.emit(72)
            self.status.emit("Parsing and validating project files...")

            try:
                bundle = extract_json(raw)
                files = validate_project_bundle(bundle)

            except Exception as first_error:
                self.status.emit(
                    "Requesting one repair pass for the project bundle..."
                )

                repair_prompt = f"""
The following model output failed project bundle validation.

Fix it and return ONLY valid JSON with shape:
{{"files": {{"relative/path": "complete file content"}}}}

Ensure all required Vite + React + TypeScript files exist,
package.json is valid, and dev/build scripts exist.

Validation error:
{first_error}

Previous output:
{raw[:90000]}
"""

                repaired = client.generate(
                    self.model,
                    SYSTEM_PROMPT,
                    repair_prompt,
                    self.token.emit,
                )

                files = validate_project_bundle(
                    extract_json(repaired)
                )

            self.progress.emit(82)
            self.status.emit("Writing project files...")

            folder = create_project_folder(self.project_name)

            for relative, content in files.items():
                destination = folder / relative

                # Re-check the resolved destination before writing.
                resolved_destination = destination.resolve()
                resolved_folder = folder.resolve()

                if (
                    resolved_destination != resolved_folder
                    and resolved_folder not in resolved_destination.parents
                ):
                    raise ValueError(
                        f"Generated path escapes project folder: {relative}"
                    )

                destination.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                destination.write_text(
                    content,
                    encoding="utf-8",
                    newline="\n",
                )

            metadata = {
                "app": APP_NAME,
                "version": APP_VERSION,
                "project_name": self.project_name,
                "model": self.model,
                "stack": "Vite + React + TypeScript",
                "architecture": self.architecture,
                "theme": self.theme,
                "features": self.features,
                "extra": self.extra,
                "entry": "index.html",
                "files": sorted(files.keys()),
            }

            (folder / ".gloxweb.json").write_text(
                json.dumps(
                    metadata,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.progress.emit(100)
            self.status.emit(
                f"Generated {len(files)} project files successfully."
            )

            self.completed.emit(
                str(folder),
                str(folder / "index.html"),
                len(files),
            )

        except Exception as error:
            self.failed.emit(str(error))


# ============================================================
# NPM COMMAND WORKER
# ============================================================

class CommandWorker(QObject):
    status = Signal(str)
    completed = Signal(bool, str)

    def __init__(self, folder, command):
        super().__init__()
        self.folder = folder
        self.command = command

    def run(self):
        try:
            if not shutil.which("npm"):
                raise RuntimeError(
                    "Node.js/npm was not found. Install the current "
                    "Node.js LTS release, then restart GloxWeb."
                )

            if self.command == "install":
                npm_args = ["install"]
                self.status.emit(
                    "Installing dependencies with npm install..."
                )

            else:
                npm_args = ["run", "build"]
                self.status.emit("Running the production build...")

            if sys.platform.startswith("win"):
                # npm is usually npm.cmd on Windows.
                command_text = subprocess.list2cmdline(
                    ["npm", *npm_args]
                )
                args = [
                    "cmd.exe",
                    "/d",
                    "/s",
                    "/c",
                    command_text,
                ]
            else:
                args = ["npm", *npm_args]

            result = subprocess.run(
                args,
                cwd=self.folder,
                capture_output=True,
                text=True,
                timeout=900,
                shell=False,
            )

            log = result.stdout or ""

            if result.stderr:
                log += "\n" + result.stderr

            if result.returncode != 0:
                raise RuntimeError(
                    log[-12000:]
                    or f"Command failed with exit code {result.returncode}."
                )

            self.completed.emit(
                True,
                log[-12000:] or "Command completed successfully.",
            )

        except subprocess.TimeoutExpired:
            self.completed.emit(
                False,
                "The npm command timed out after 15 minutes.",
            )

        except Exception as error:
            self.completed.emit(False, str(error))


# ============================================================
# MAIN WINDOW
# ============================================================

class GloxWebWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.worker_thread = None
        self.worker = None

        self.model_loader = None

        self.command_thread = None
        self.command_worker = None

        self.current_project_folder = None
        self.busy = False

        self.setWindowTitle(f"GloxWeb {APP_VERSION}")
        self.resize(1480, 940)
        self.setMinimumSize(1100, 720)

        self.build_ui()
        self.load_models()

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()

        title = QLabel("GLOXWEB")
        title.setFont(QFont("Arial", 24, QFont.Weight.Bold))

        subtitle = QLabel(
            "AI PROJECT GENERATOR  •  VITE + REACT + TYPESCRIPT"
        )
        subtitle.setObjectName("subtitle")

        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        header.addLayout(title_box)
        header.addStretch()

        self.api_status = QLabel("OPENROUTER  •  CHECKING")
        self.api_status.setObjectName("apiStatus")
        header.addWidget(self.api_status)

        root.addLayout(header)

        # Main panels
        main = QHBoxLayout()
        main.setSpacing(14)

        left = QFrame()
        left.setObjectName("panel")
        left.setMinimumWidth(440)

        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(18, 18, 18, 18)
        left_layout.setSpacing(9)

        self.add_section(left_layout, "PROJECT")

        left_layout.addWidget(QLabel("Project Name"))

        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("CampusConnect")
        left_layout.addWidget(self.project_name)

        left_layout.addWidget(QLabel("Free OpenRouter Model"))

        model_row = QHBoxLayout()

        self.model_combo = QComboBox()
        model_row.addWidget(self.model_combo, 1)

        self.refresh_models = QPushButton("REFRESH")
        self.refresh_models.setFixedWidth(95)
        self.refresh_models.clicked.connect(self.load_models)
        model_row.addWidget(self.refresh_models)

        left_layout.addLayout(model_row)

        self.model_info = QLabel("Select a model after loading.")
        self.model_info.setObjectName("smallInfo")
        self.model_info.setWordWrap(True)
        left_layout.addWidget(self.model_info)

        self.add_section(left_layout, "SPECIFICATION")

        left_layout.addWidget(QLabel("Website Architecture"))

        self.architecture = QTextEdit()
        self.architecture.setPlaceholderText(
            "Pages, sections, user flow, target audience..."
        )
        self.architecture.setFixedHeight(100)
        left_layout.addWidget(self.architecture)

        left_layout.addWidget(QLabel("Theme and Design"))

        self.theme = QTextEdit()
        self.theme.setPlaceholderText(
            "Palette, typography, layout, mood, references..."
        )
        self.theme.setFixedHeight(85)
        left_layout.addWidget(self.theme)

        left_layout.addWidget(QLabel("Features and Interactions"))

        self.features = QTextEdit()
        self.features.setPlaceholderText(
            "Forms, search, filters, local state, navigation..."
        )
        self.features.setFixedHeight(90)
        left_layout.addWidget(self.features)

        left_layout.addWidget(QLabel("Extra Instructions"))

        self.extra = QTextEdit()
        self.extra.setPlaceholderText(
            "Additional constraints or details..."
        )
        self.extra.setFixedHeight(70)
        left_layout.addWidget(self.extra)

        left_layout.addStretch()

        self.generate_button = QPushButton("GENERATE VITE PROJECT")
        self.generate_button.setObjectName("generateButton")
        self.generate_button.setMinimumHeight(48)
        self.generate_button.clicked.connect(self.generate_project)
        left_layout.addWidget(self.generate_button)

        main.addWidget(left, 1)

        # Output panel
        right = QFrame()
        right.setObjectName("panel")

        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(18, 18, 18, 18)
        right_layout.setSpacing(10)

        output_header = QHBoxLayout()

        output_title = QLabel("GENERATION LOG")
        output_title.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        output_header.addWidget(output_title)
        output_header.addStretch()

        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("statusLabel")
        output_header.addWidget(self.status_label)

        right_layout.addLayout(output_header)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 9))
        self.output.setPlaceholderText(
            "Generation stream and project logs appear here..."
        )
        right_layout.addWidget(self.output, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        right_layout.addWidget(self.progress)

        buttons = QHBoxLayout()

        self.open_project_button = QPushButton("OPEN PROJECT FOLDER")
        self.open_project_button.setEnabled(False)
        self.open_project_button.clicked.connect(self.open_project)
        buttons.addWidget(self.open_project_button)

        self.install_button = QPushButton("INSTALL DEPENDENCIES")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self.install_dependencies)
        buttons.addWidget(self.install_button)

        self.build_button = QPushButton("CHECK PRODUCTION BUILD")
        self.build_button.setEnabled(False)
        self.build_button.clicked.connect(self.build_project)
        buttons.addWidget(self.build_button)

        self.run_button = QPushButton("RUN DEV SERVER")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_dev_server)
        buttons.addWidget(self.run_button)

        right_layout.addLayout(buttons)

        main.addWidget(right, 2)
        root.addLayout(main, 1)

    def add_section(self, layout, text):
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        layout.addWidget(label)

    # --------------------------------------------------------
    # BUTTON STATE
    # --------------------------------------------------------

    def set_busy(self, busy):
        self.busy = busy

        self.generate_button.setEnabled(not busy)
        self.refresh_models.setEnabled(not busy)

        has_project = bool(
            self.current_project_folder
            and Path(self.current_project_folder).exists()
        )

        self.open_project_button.setEnabled(
            not busy and has_project
        )
        self.install_button.setEnabled(
            not busy and has_project
        )
        self.build_button.setEnabled(
            not busy and has_project
        )
        self.run_button.setEnabled(
            not busy and has_project
        )

    # --------------------------------------------------------
    # MODEL LOADING
    # --------------------------------------------------------

    def load_models(self):
        if self.busy:
            return

        if not OPENROUTER_API_KEY:
            self.api_status.setText("OPENROUTER  •  NO API KEY")
            self.model_combo.clear()
            self.model_combo.addItem("No API key")

            self.model_info.setText(
                "Add OPENROUTER_API_KEY to the .env file "
                "beside GloxWeb.exe."
            )
            return

        self.refresh_models.setEnabled(False)
        self.api_status.setText("OPENROUTER  •  LOADING")
        self.status_label.setText("Detecting free models...")
        self.model_info.setText(
            "Fetching models from OpenRouter..."
        )

        self.model_combo.clear()
        self.model_combo.addItem("Loading free models...")

        self.model_loader = ModelLoader(OPENROUTER_API_KEY)
        self.model_loader.loaded.connect(self.models_loaded)
        self.model_loader.failed.connect(self.models_failed)
        self.model_loader.finished.connect(
            self.model_loader.deleteLater
        )
        self.model_loader.start()

    def models_loaded(self, models):
        self.model_combo.clear()

        for model in models:
            display = f"{model['name']}  •  {model['id']}"
            self.model_combo.addItem(display, model["id"])

            index = self.model_combo.count() - 1
            context = model.get("context_length")

            tip = f"Model: {model['name']}\nID: {model['id']}"

            if context:
                tip += f"\nContext: {context:,}"

            self.model_combo.setItemData(
                index,
                tip,
                Qt.ItemDataRole.ToolTipRole,
            )

        self.model_combo.setCurrentIndex(
            0 if models else -1
        )

        self.model_info.setText(
            f"{len(models)} free models found. Choose one that "
            "can handle long structured output."
            if models
            else "No free models were returned by OpenRouter."
        )

        self.refresh_models.setEnabled(not self.busy)
        self.api_status.setText("OPENROUTER  •  CONNECTED")
        self.status_label.setText(
            "Choose a model and describe your project."
        )

    def models_failed(self, error):
        self.refresh_models.setEnabled(not self.busy)
        self.api_status.setText("OPENROUTER  •  ERROR")
        self.status_label.setText("Model detection failed.")
        self.model_info.setText(
            "Could not retrieve the model list."
        )

        self.model_combo.clear()
        self.model_combo.addItem("Model loading failed")

        QMessageBox.warning(
            self,
            "OpenRouter Model Detection",
            error,
        )

    def current_model(self):
        model = self.model_combo.currentData()

        if not model:
            raise RuntimeError(
                "Select a valid OpenRouter model first."
            )

        return str(model).strip()

    # --------------------------------------------------------
    # GENERATE PROJECT
    # --------------------------------------------------------

    def generate_project(self):
        if self.busy:
            return

        if not OPENROUTER_API_KEY:
            QMessageBox.warning(
                self,
                "OpenRouter",
                "OPENROUTER_API_KEY is missing from your .env file.",
            )
            return

        name = self.project_name.text().strip()

        if not name:
            QMessageBox.warning(
                self,
                "Project Name",
                "Enter a project name.",
            )
            self.project_name.setFocus()
            return

        try:
            model = self.current_model()
        except RuntimeError as error:
            QMessageBox.warning(
                self,
                "Model Selection",
                str(error),
            )
            return

        architecture = (
            self.architecture.toPlainText().strip()
            or "A polished modern website with clear navigation, "
               "a hero, relevant content sections, and footer."
        )

        theme = (
            self.theme.toPlainText().strip()
            or "Distinctive modern design, strong typography, "
               "responsive layouts, intentional spacing, and "
               "refined interactions."
        )

        features = (
            self.features.toPlainText().strip()
            or "Functional navigation, meaningful buttons, "
               "responsive menu, and useful interactive states."
        )

        extra = self.extra.toPlainText().strip()

        self.output.clear()
        self.progress.setValue(0)
        self.set_busy(True)

        self.status_label.setText(
            f"Starting generation with {model}..."
        )

        self.worker_thread = QThread()
        self.worker = GenerationWorker(
            name,
            model,
            architecture,
            theme,
            features,
            extra,
        )

        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)

        self.worker.token.connect(self.append_output)
        self.worker.status.connect(self.set_status)
        self.worker.progress.connect(self.progress.setValue)

        self.worker.completed.connect(self.generation_finished)
        self.worker.failed.connect(self.generation_failed)

        self.worker.completed.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)

        self.worker_thread.finished.connect(
            self.worker.deleteLater
        )
        self.worker_thread.finished.connect(
            self.worker_thread.deleteLater
        )

        self.worker_thread.start()

    def append_output(self, chunk):
        self.output.moveCursor(
            self.output.textCursor().MoveOperation.End
        )
        self.output.insertPlainText(chunk)
        self.output.ensureCursorVisible()

    def set_status(self, status):
        self.status_label.setText(status)

    def generation_finished(self, folder, index_file, file_count):
        self.current_project_folder = folder
        self.set_busy(False)

        self.progress.setValue(100)
        self.status_label.setText(
            f"Generated {file_count} files."
        )

        self.output.append(
            "\n\n--- PROJECT CREATED ---\n"
            f"Folder: {folder}\n"
            f"Files: {file_count}\n"
            "Stack: Vite + React + TypeScript\n\n"
            "Next: click INSTALL DEPENDENCIES, then RUN DEV SERVER."
        )

        QMessageBox.information(
            self,
            "GloxWeb",
            f"Project generated successfully.\n\n"
            f"Files: {file_count}\n"
            f"Project:\n{folder}\n\n"
            "Next, install dependencies before running it.",
        )

    def generation_failed(self, error):
        self.set_busy(False)
        self.status_label.setText("Generation failed.")
        self.output.append("\n\n--- ERROR ---\n" + error)

        QMessageBox.critical(
            self,
            "GloxWeb Error",
            error,
        )

    # --------------------------------------------------------
    # OPEN PROJECT
    # --------------------------------------------------------

    def open_project(self):
        if self.busy or not self.current_project_folder:
            return

        folder = Path(self.current_project_folder)

        if not folder.exists():
            QMessageBox.warning(
                self,
                "Project",
                "The project folder no longer exists.",
            )
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as error:
            QMessageBox.warning(
                self,
                "Open Project",
                str(error),
            )

    # --------------------------------------------------------
    # NPM COMMANDS
    # --------------------------------------------------------

    def start_command_worker(self, command):
        if self.busy or not self.current_project_folder:
            return

        folder = Path(self.current_project_folder)

        if not folder.exists():
            QMessageBox.warning(
                self,
                "Project",
                "The project folder no longer exists.",
            )
            return

        self.set_busy(True)

        self.command_thread = QThread()
        self.command_worker = CommandWorker(
            str(folder),
            command,
        )

        self.command_worker.moveToThread(
            self.command_thread
        )

        self.command_thread.started.connect(
            self.command_worker.run
        )

        self.command_worker.status.connect(self.set_status)
        self.command_worker.completed.connect(
            self.command_finished
        )

        self.command_worker.completed.connect(
            self.command_thread.quit
        )

        self.command_thread.finished.connect(
            self.command_worker.deleteLater
        )
        self.command_thread.finished.connect(
            self.command_thread.deleteLater
        )

        self.command_thread.start()

    def install_dependencies(self):
        if self.busy or not self.current_project_folder:
            return

        package_file = (
            Path(self.current_project_folder) / "package.json"
        )

        if not package_file.exists():
            QMessageBox.warning(
                self,
                "Project",
                "package.json is missing.",
            )
            return

        self.start_command_worker("install")

    def build_project(self):
        if self.busy or not self.current_project_folder:
            return

        folder = Path(self.current_project_folder)

        if not (folder / "node_modules").exists():
            QMessageBox.information(
                self,
                "Dependencies",
                "Install dependencies first.",
            )
            return

        self.start_command_worker("build")

    def command_finished(self, success, message):
        self.output.append(
            (
                "\n\n--- COMMAND SUCCEEDED ---\n"
                if success
                else "\n\n--- COMMAND FAILED ---\n"
            )
            + message
        )

        self.status_label.setText(
            "Command completed successfully."
            if success
            else "Command failed."
        )

        self.set_busy(False)

        if not success:
            QMessageBox.warning(
                self,
                "GloxWeb Command",
                message[-3000:],
            )
        else:
            QMessageBox.information(
                self,
                "GloxWeb Command",
                message[-3000:],
            )

    # --------------------------------------------------------
    # RUN DEV SERVER
    # --------------------------------------------------------

    def run_dev_server(self):
        if self.busy or not self.current_project_folder:
            return

        folder = Path(self.current_project_folder)

        if not (folder / "package.json").exists():
            QMessageBox.warning(
                self,
                "Project",
                "package.json is missing.",
            )
            return

        if not (folder / "node_modules").exists():
            QMessageBox.information(
                self,
                "Dependencies",
                "Click INSTALL DEPENDENCIES first.",
            )
            return

        if not shutil.which("npm"):
            QMessageBox.warning(
                self,
                "Node.js",
                "Install Node.js LTS and restart GloxWeb.",
            )
            return

        try:
            if sys.platform.startswith("win"):
                flags = getattr(
                    subprocess,
                    "CREATE_NEW_CONSOLE",
                    0,
                )

                subprocess.Popen(
                    [
                        "cmd.exe",
                        "/d",
                        "/s",
                        "/c",
                        "npm run dev -- --open",
                    ],
                    cwd=folder,
                    creationflags=flags,
                    shell=False,
                )
            else:
                subprocess.Popen(
                    ["npm", "run", "dev", "--", "--open"],
                    cwd=folder,
                    start_new_session=True,
                )

            self.status_label.setText(
                "Starting Vite dev server..."
            )

            self.output.append(
                "\nStarted npm run dev. Check the terminal "
                "for the local URL if the browser does not open."
            )

        except Exception as error:
            QMessageBox.critical(
                self,
                "Dev Server",
                str(error),
            )


# ============================================================
# APPLICATION STYLE
# ============================================================

APP_STYLE = """
QMainWindow {
    background: #0b0d10;
}

QWidget {
    color: #eeeeee;
    font-family: Arial;
    font-size: 13px;
}

QFrame#panel {
    background: #111419;
    border: 1px solid #252a31;
    border-radius: 12px;
}

QLabel {
    color: #eeeeee;
}

QLabel#subtitle {
    color: #858b94;
    font-size: 11px;
}

QLabel#sectionLabel {
    color: #ff4b4b;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 2px;
    margin-top: 4px;
}

QLabel#apiStatus {
    background: #15191f;
    border: 1px solid #292f37;
    border-radius: 7px;
    padding: 7px 12px;
    color: #aeb5bf;
    font-size: 11px;
    font-weight: bold;
}

QLabel#statusLabel {
    color: #8e96a1;
    font-size: 11px;
}

QLabel#smallInfo {
    color: #9aa2ad;
    font-size: 10px;
}

QLineEdit, QTextEdit, QComboBox {
    background: #0b0e12;
    border: 1px solid #2a3038;
    border-radius: 7px;
    padding: 8px;
    color: #eeeeee;
    selection-background-color: #343b45;
}

QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
    border: 1px solid #626b78;
}

QComboBox {
    min-height: 22px;
}

QComboBox::drop-down {
    border: none;
    width: 28px;
}

QComboBox QAbstractItemView {
    background: #111419;
    border: 1px solid #303640;
    color: #eeeeee;
    selection-background-color: #292f38;
    selection-color: #ffffff;
    padding: 4px;
}

QPushButton {
    background: #181d23;
    border: 1px solid #303640;
    border-radius: 7px;
    padding: 9px 12px;
    color: #eeeeee;
    font-weight: bold;
}

QPushButton:hover {
    background: #20262d;
}

QPushButton:pressed {
    background: #101318;
}

QPushButton:disabled {
    color: #555d68;
    background: #121519;
}

QPushButton#generateButton {
    background: #eeeeee;
    color: #0b0d10;
    border: none;
    font-size: 13px;
    font-weight: bold;
}

QPushButton#generateButton:hover {
    background: #ffffff;
}

QProgressBar {
    background: #0b0e12;
    border: 1px solid #292f37;
    border-radius: 6px;
    height: 14px;
    text-align: center;
    color: #eeeeee;
}

QProgressBar::chunk {
    background: #eeeeee;
    border-radius: 5px;
}

QScrollBar:vertical {
    background: #0b0e12;
    width: 10px;
}

QScrollBar::handle:vertical {
    background: #303640;
    min-height: 30px;
    border-radius: 5px;
}

QScrollBar:horizontal {
    background: #0b0e12;
    height: 10px;
}

QScrollBar::handle:horizontal {
    background: #303640;
    border-radius: 5px;
}
"""


# ============================================================
# MAIN
# ============================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(APP_STYLE)

    window = GloxWebWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()