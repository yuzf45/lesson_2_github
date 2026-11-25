print(
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration


# --- Пути проекта ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
FONTS_DIR = BASE_DIR / "fonts"

DATA_EXTENSIONS = {".csv", ".json"}
TEMPLATE_EXTENSIONS = {".html", ".htm"}

# Возможные названия столбца / поля с номером счёта
INVOICE_ID_CANDIDATES = (
    "invoiceid",
    "invoice",
    "checkid",
    "receiptid",
    "id",
)


def print_title(text: str) -> None:
    print("\n" + "=" * 70)
    print(f" {text}")
    print("=" * 70)


def normalize_key(value: Any) -> str:
    """Приводит название поля к виду invoiceid, checkid и т. д."""
    return re.sub(r"[^a-zA-Zа-яА-Я0-9]", "", str(value)).lower()


def choose_from_menu(items: list[Path], prompt: str) -> Path:
    """Показывает нумерованное меню и возвращает выбранный путь."""
    for index, item in enumerate(items, start=1):
        print(f"  {index}. {item.name}")

    while True:
        choice = input(f"\n{prompt}: ").strip()

        if not choice.isdigit():
            print("Введите номер варианта.")
            continue

        number = int(choice)
        if 1 <= number <= len(items):
            return items[number - 1]

        print(f"Введите число от 1 до {len(items)}.")


def find_data_files() -> list[Path]:
    if not DATA_DIR.exists():
        raise FileNotFoundError(
            f"Не найдена папка с данными: {DATA_DIR}\n"
            "Создайте папку data рядом со скриптом."
        )

    return sorted(
        path for path in DATA_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in DATA_EXTENSIONS
    )


def find_templates() -> list[Path]:
    if not TEMPLATES_DIR.exists():
        raise FileNotFoundError(
            f"Не найдена папка шаблонов: {TEMPLATES_DIR}\n"
            "Создайте папку templates рядом со скриптом."
        )

    return sorted(
        path for path in TEMPLATES_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in TEMPLATE_EXTENSIONS
    )


def load_csv(file_path: Path) -> list[dict[str, Any]]:
    """Читает CSV с попыткой определить кодировку и разделитель."""
    encodings = ("utf-8-sig", "utf-8", "cp1251")

    last_error: Exception | None = None

    for encoding in encodings:
        try:
            dataframe = pd.read_csv(
                file_path,
                encoding=encoding,
                sep=None,
                engine="python",
            )

            # Заменяем NaN на None, чтобы в шаблон не попадало "nan"
            dataframe = dataframe.where(pd.notna(dataframe), None)
            return dataframe.to_dict(orient="records")

        except Exception as error:
            last_error = error

    raise ValueError(
        f"Не удалось прочитать CSV-файл: {file_path.name}\n"
        f"Причина: {last_error}"
    )


def records_from_json(data: Any) -> list[dict[str, Any]]:
    """Преобразует распространённые JSON-структуры в список записей."""
    if isinstance(data, list):
        if not all(isinstance(item, dict) for item in data):
            raise ValueError("JSON-массив должен состоять из объектов.")
        return data

    if isinstance(data, dict):
        # Частые названия контейнеров с записями
        for key in ("invoices", "checks", "receipts", "records", "items", "data"):
            value = data.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value

        # JSON вида {"INV-001": {...}, "INV-002": {...}}
        if data and all(isinstance(value, dict) for value in data.values()):
            result = []
            for invoice_id, record in data.items():
                record_copy = dict(record)

                if not find_invoice_id_field(record_copy):
                    record_copy["invoice_id"] = invoice_id

                result.append(record_copy)

            return result

        # Один объект-счёт
        return [data]

    raise ValueError("Поддерживаются JSON-объекты или массивы объектов.")


def load_json(file_path: Path) -> list[dict[str, Any]]:
    try:
        with file_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Ошибка формата JSON в файле {file_path.name}: {error}"
        ) from error

    return records_from_json(data)


def load_data_file(file_path: Path) -> list[dict[str, Any]]:
    if file_path.suffix.lower() == ".csv":
        records = load_csv(file_path)
    elif file_path.suffix.lower() == ".json":
        records = load_json(file_path)
    else:
        raise ValueError(f"Неподдерживаемый формат: {file_path.suffix}")

    if not records:
        raise ValueError(f"Файл {file_path.name} не содержит записей.")

    return records


def find_invoice_id_field(record: dict[str, Any]) -> str | None:
    """Ищет поле, которое содержит идентификатор счёта."""
    normalized_fields = {
        normalize_key(field_name): field_name
        for field_name in record.keys()
    }

    for candidate in INVOICE_ID_CANDIDATES:
        if candidate in normalized_fields:
            return str(normalized_fields[candidate])

    return None


def get_invoice_groups(
    records: list[dict[str, Any]]
) -> tuple[str, OrderedDict[str, list[dict[str, Any]]]]:
    """Группирует записи по invoice id."""
    id_field: str | None = None

    for record in records:
        id_field = find_invoice_id_field(record)
        if id_field:
            break

    if not id_field:
        available_fields = ", ".join(
            str(key) for key in records[0].keys()
        )
        raise ValueError(
            "Не найдено поле с идентификатором счёта.\n"
            "Ожидаемые варианты: invoice_id, invoice id, invoice, "
            "check_id, receipt_id или id.\n"
            f"Доступные поля: {available_fields}"
        )

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    for record in records:
        value = record.get(id_field)

        if value is None or str(value).strip() == "":
            continue

        invoice_id = str(value).strip()
        grouped.setdefault(invoice_id, []).append(record)

    if not grouped:
        raise ValueError(
            f"В поле «{id_field}» не найдено ни одного invoice id."
        )

    return id_field, grouped


def choose_invoice(
    grouped_invoices: OrderedDict[str, list[dict[str, Any]]]
) -> tuple[str, list[dict[str, Any]]]:
    print_title("ДОСТУПНЫЕ ЧЕКИ / СЧЕТА")

    invoice_ids = list(grouped_invoices.keys())

    for index, invoice_id in enumerate(invoice_ids, start=1):
        records_count = len(grouped_invoices[invoice_id])
        suffix = "" if records_count == 1 else f" — строк: {records_count}"
        print(f"  {index}. {invoice_id}{suffix}")

    while True:
        choice = input("\nВыберите номер invoice id: ").strip()

        if not choice.isdigit():
            print("Введите номер варианта.")
            continue

        number = int(choice)
        if 1 <= number <= len(invoice_ids):
            selected_id = invoice_ids[number - 1]
            return selected_id, grouped_invoices[selected_id]

        print(f"Введите число от 1 до {len(invoice_ids)}.")


def find_cyrillic_font() -> Path:
    """Ищет DejaVu Sans или Roboto в папке проекта и системе."""
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

    font_directories = [
        FONTS_DIR,
        windows_fonts,
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/truetype/roboto"),
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
    ]

    file_names = (
        "DejaVuSans.ttf",
        "DejaVuSans-Regular.ttf",
        "Roboto-Regular.ttf",
    )

    for directory in font_directories:
        if not directory.exists():
            continue

        for file_name in file_names:
            direct_path = directory / file_name
            if direct_path.exists():
                return direct_path

        # Поиск в подпапках, например /Library/Fonts/Microsoft
        for file_name in file_names:
            found = list(directory.rglob(file_name))
            if found:
                return found[0]

    raise FileNotFoundError(
        "Не найден шрифт DejaVu Sans или Roboto.\n"
        "Поместите файл DejaVuSans.ttf в папку fonts рядом со скриптом."
    )


def safe_filename(value: str) -> str:
    """Делает значение пригодным для имени файла Windows и macOS."""
    cleaned = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]', "_", value)
    cleaned = cleaned.strip(" ._")
    return cleaned[:100] or "invoice"


def open_pdf(pdf_path: Path) -> None:
    """Открывает PDF в программе по умолчанию."""
    system_name = platform.system()

    try:
        if system_name == "Windows":
            os.startfile(str(pdf_path))  # type: ignore[attr-defined]
        elif system_name == "Darwin":
            subprocess.run(["open", str(pdf_path)], check=False)
        else:
            subprocess.run(["xdg-open", str(pdf_path)], check=False)
    except Exception as error:
        print(f"Не удалось автоматически открыть PDF: {error}")


def generate_pdf(
    template_path: Path,
    invoice_id: str,
    invoice_records: list[dict[str, Any]],
    source_file: Path,
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    font_path = find_cyrillic_font()
    font_configuration = FontConfiguration()

    # Корень шаблонов позволяет использовать {% include %} и относительные файлы
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "htm", "xml"]),
    )

    template_name = str(template_path.relative_to(TEMPLATES_DIR)).replace("\\", "/")
    template = environment.get_template(template_name)

    # invoice — первая строка счёта.
    # items / records — все строки, относящиеся к выбранному invoice id.
    html_content = template.render(
        invoice=invoice_records[0],
        items=invoice_records,
        records=invoice_records,
        invoice_id=invoice_id,
        source_file=source_file.name,
        **invoice_records[0],
    )

    font_css = f"""
    @font-face {{
        font-family: "InvoiceFont";
        src: url("{font_path.resolve().as_uri()}") format("truetype");
    }}

    html, body {{
        font-family: "InvoiceFont", sans-serif;
    }}
    """

    output_name = f"invoice_{safe_filename(invoice_id)}.pdf"
    output_path = OUTPUT_DIR / output_name

    HTML(
        string=html_content,
        base_url=str(template_path.parent.resolve()),
    ).write_pdf(
        str(output_path),
        stylesheets=[
            CSS(string=font_css, font_config=font_configuration),
        ],
        font_config=font_configuration,
    )

    return output_path


def main() -> None:
    print_title("ГЕНЕРАТОР PDF-ЧЕКОВ")

    data_files = find_data_files()
    templates = find_templates()

    if not data_files:
        print(f"В папке {DATA_DIR} не найдены CSV или JSON-файлы.")
        return

    if not templates:
        print(f"В папке {TEMPLATES_DIR} не найдены HTML-шаблоны.")
        return

    print_title("ДОСТУПНЫЕ ФАЙЛЫ ДАННЫХ")
    data_file = choose_from_menu(data_files, "Выберите номер файла данных")

    print_title("ДОСТУПНЫЕ HTML-ШАБЛОНЫ")
    template_file = choose_from_menu(templates, "Выберите номер HTML-шаблона")

    print_title("ЗАГРУЗКА ДАННЫХ")
    print(f"Файл данных: {data_file.name}")
    print(f"Шаблон:      {template_file.name}")

    records = load_data_file(data_file)
    _, grouped_invoices = get_invoice_groups(records)

    invoice_id, invoice_records = choose_invoice(grouped_invoices)

    print_title("СОЗДАНИЕ PDF")
    print(f"Invoice ID: {invoice_id}")
    print("Генерация PDF...")

    pdf_path = generate_pdf(
        template_path=template_file,
        invoice_id=invoice_id,
        invoice_records=invoice_records,
        source_file=data_file,
    )

    print(f"\nPDF успешно создан:\n{pdf_path}")
    open_pdf(pdf_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nРабота отменена пользователем.")
        sys.exit(1)
    except Exception as error:
        print(f"\nОшибка: {error}")
        sys.exit(1)
        )