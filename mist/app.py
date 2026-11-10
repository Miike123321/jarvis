import os
import sys
import subprocess
from pathlib import Path

REQUIRED_PACKAGES = {
    "streamlit": "streamlit",
    "pandas": "pandas",
    "openpyxl": "openpyxl",
    "xlrd": "xlrd"
    
}

def install_and_import(package_import_name, package_pip_name):
    try:
        __import__(package_import_name)
    except ImportError:
        print(f"Пакет '{package_pip_name}' не найден. Установка...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_pip_name])

for import_name, pip_name in REQUIRED_PACKAGES.items():
    install_and_import(import_name, pip_name)

if not os.environ.get("STREAMLIT_RUNNING"):
    os.environ["STREAMLIT_RUNNING"] = "1"
    try:
        from streamlit.web import cli as stcli
        sys.argv = ["streamlit", "run", sys.argv[0]]
        sys.exit(stcli.main())
    except Exception:
        subprocess.run([sys.executable, "-m", "streamlit", "run", sys.argv[0]])
        sys.exit()

import datetime
import io
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Сборка бонусов Meest", layout="centered")
st.title("📦 Расчет бонусов Meest Экспресс")

def parse_register_file(file):
    df = pd.read_excel(file, header=None)
    
    hdr_r = None
    for r in range(min(10, df.shape[0])):
        row_str = [str(x) for x in df.iloc[r].values if pd.notna(x)]
        if 'Підрозділ' in row_str or '№ відділення' in row_str:
            hdr_r = r
            break
            
    if hdr_r is None:
        return pd.DataFrame()
        
    hdr_main_raw = df.iloc[hdr_r]
    hdr_sub_raw = df.iloc[hdr_r + 1]
    
    current_srv = None
    hdr_main_ffill = []
    for c in range(df.shape[1]):
        val = hdr_main_raw[c]
        if pd.notna(val) and str(val).strip() not in ['Підрозділ', '№ відділення', 'Виконавець', 'ЕДРПОУВиконавця']:
            current_srv = str(val).strip()
        elif pd.notna(val) and str(val).strip() in ['Підрозділ', '№ відділення', 'Виконавець', 'ЕДРПОУВиконавця']:
            current_srv = None
        hdr_main_ffill.append(current_srv)
        
    branch_col = None
    for c in range(df.shape[1]):
        if pd.notna(hdr_main_raw[c]) and str(hdr_main_raw[c]).strip() == '№ відділення':
            branch_col = c
            break
            
    rows = []
    for r in range(hdr_r + 2, df.shape[0]):
        b_val = df.iloc[r, branch_col] if branch_col is not None else None
        if pd.notna(b_val):
            try:
                b_num = int(float(b_val))
            except ValueError:
                continue
                
            row_data = {
                'Відділення': b_num,
                'sum_1kg': 0.0,
                'sum_2kg': 0.0,
                'sum_10kg': 0.0,
                'sum_over_10kg': 0.0,
                'qty_1kg': 0,
                'qty_2kg': 0,
                'qty_10kg': 0,
                'qty_over_10kg': 0,
            }
            
            for c in range(df.shape[1]):
                srv = hdr_main_ffill[c]
                sub = str(hdr_sub_raw[c]).strip() if pd.notna(hdr_sub_raw[c]) else ""
                val = df.iloc[r, c]
                if pd.isna(val) or srv is None:
                    continue
                    
                if 'Видача відправлення отримувачу до 1 кг' in srv:
                    if sub == 'Сума': row_data['sum_1kg'] = float(val)
                    elif sub == 'К-сть': row_data['qty_1kg'] = int(val)
                elif 'Видача відправлення отримувачу до 2 кг' in srv:
                    if sub == 'Сума': row_data['sum_2kg'] = float(val)
                    elif sub == 'К-сть': row_data['qty_2kg'] = int(val)
                elif 'Видача відправлення отримувачу до 10 кг' in srv:
                    if sub == 'Сума': row_data['sum_10kg'] = float(val)
                    elif sub == 'К-сть': row_data['qty_10kg'] = int(val)
                elif 'Видача відправлення отримувачу більше 10 кг' in srv:
                    if sub == 'Сума': row_data['sum_over_10kg'] = float(val)
                    elif sub == 'К-сть': row_data['qty_over_10kg'] = int(val)
                    
            rows.append(row_data)
            
    return pd.DataFrame(rows)

st.subheader("1. Динамические файлы (Реестры)")
uploaded_files = st.file_uploader("Загрузите 3 файла реестров (.xls / .xlsx)", type=["xls", "xlsx"], accept_multiple_files=True)

st.subheader("2. Статичные файлы (Справочники)")

app_directory = Path(__file__).resolve().parent
excel_files = sorted(
    (path for path in app_directory.iterdir() if path.is_file() and path.suffix.lower() in {".xlsx", ".xls"}),
    key=lambda path: path.name.lower()
)

def select_reference_file(label, candidates, preferred_name):
    if not candidates:
        st.warning(f"В папке приложения не найден файл для выбора: {label}.")
        selected_file = None
    else:
        preferred_name = preferred_name.casefold()
        default_index = next(
            (index for index, path in enumerate(candidates) if path.name.casefold() == preferred_name),
            0
        )
        selected_file = st.selectbox(
            label,
            options=candidates,
            index=default_index,
            format_func=lambda path: path.name
        )

    return selected_file

stores_candidates = [
    path for path in excel_files
    if "магазин" in path.name.casefold()
    and "директор" not in path.name.casefold()
    and "деректор" not in path.name.casefold()
]
directors_candidates = [
    path for path in excel_files
    if "директор" in path.name.casefold() or "деректор" in path.name.casefold()
]

stores_ref_file = select_reference_file(
    "Файл со справочником магазинов",
    stores_candidates,
    "магазины мист.xlsx"
)
directors_ref_file = select_reference_file(
    "Файл со справочником директоров",
    directors_candidates,
    "деректора магазинов.xlsx"
)

with st.expander("Заменить справочник файлом с компьютера"):
    stores_ref_upload = st.file_uploader(
        "Другой файл магазинов (необязательно)",
        type=["xlsx", "xls"],
        key="stores_ref_upload"
    )
    directors_ref_upload = st.file_uploader(
        "Другой файл директоров (необязательно)",
        type=["xlsx", "xls"],
        key="directors_ref_upload"
    )

if stores_ref_upload is not None:
    stores_ref_file = stores_ref_upload
if directors_ref_upload is not None:
    directors_ref_file = directors_ref_upload

if st.button("Сформировать отчет"):
    if len(uploaded_files) != 3:
        st.error("Пожалуйста, загрузите ровно 3 файла реестров!")
    elif not stores_ref_file or not directors_ref_file:
        st.error("Пожалуйста, загрузите оба статичных справочника!")
    else:
        # Парсинг 3 реестров
        list_dfs = [parse_register_file(f) for f in uploaded_files]
        df_registers = pd.concat(list_dfs, ignore_index=True)
        
        df_aggregated = df_registers.groupby('Відділення', as_index=False).sum()
        df_aggregated['Кіль-ть видач'] = (
            df_aggregated['qty_1kg'] + df_aggregated['qty_2kg']
            + df_aggregated['qty_10kg'] + df_aggregated['qty_over_10kg']
        )
        df_aggregated['Бонусы_суммарно'] = (
            df_aggregated['sum_1kg'] + df_aggregated['sum_2kg']
            + df_aggregated['sum_10kg'] + df_aggregated['sum_over_10kg']
        )
        
        df_stores_ref = pd.read_excel(stores_ref_file)
        df_directors_ref = pd.read_excel(directors_ref_file)
        
        merged = df_stores_ref.merge(df_aggregated, on='Відділення', how='left').fillna(0)
        
        # Карта постоянного сопоставления отделений Meest и Табельных номеров директоров
        branch_to_tab = {
            18301: 3639, 18302: 13224, 18304: 47456, 18283: 45928, 18284: 76343,
            18269: 38262, 18268: 11970, 18270: 12797, 22482: 38016, 18282: 68694,
            22483: 76218, 22478: 66552, 18274: 56769, 18273: 71971, 22480: 75339,
            18275: 8642, 18280: 13729, 18279: 46564, 22481: 42120, 18307: 54521,
            22479: 72304, 18300: 15371, 19936: 64801, 19950: 46771, 19935: 47251,
            18286: 8634, 19939: 16508, 18308: 72362
        }
        
        merged['Табельный номер'] = merged['Відділення'].map(branch_to_tab)
        
        # Подтягивание ФИО директоров по табельному номеру из справочника директоров
        df_directors_ref_clean = df_directors_ref.dropna(subset=['Табельний номер']).copy()
        df_directors_ref_clean['Табельный номер'] = df_directors_ref_clean['Табельний номер'].astype(int)
        
        final_df = merged.merge(df_directors_ref_clean[['Табельный номер', 'ПІБ']], on='Табельный номер', how='left')
        
        # Определение наименования прошлого месяца на украинском
        ukr_months_upper = [
            "СІЧЕНЬ", "ЛЮТИЙ", "БЕРЕЗЕНЬ", "КВІТЕНЬ", "ТРАВЕНЬ", "ЧЕРВЕНЬ",
            "ЛИПЕНЬ", "СЕРПЕНЬ", "ВЕРЕСЕНЬ", "ЖОВТЕНЬ", "ЛИСТОПАД", "ГРУДЕНЬ"
        ]
        ukr_months_lower = [
            "січень", "лютий", "березень", "квітень", "травень", "червень",
            "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"
        ]
        
        today = datetime.date.today()
        first_day_this_month = today.replace(day=1)
        last_month_date = first_day_this_month - datetime.timedelta(days=1)
        prev_month_idx = last_month_date.month - 1
        
        bonus_col_name = f"Бонуси за {ukr_months_upper[prev_month_idx]}, грн"
        output_filename = f"бонусы {ukr_months_lower[prev_month_idx]}.xlsx"
        
        # Формирование таблицы в точности как в образце
        final_report = pd.DataFrame({
            '№': range(1, len(final_df) + 1),
            'Магазин': final_df['Магазин'],
            'Відділення': final_df['Відділення'],
            'Кіль-ть видач': final_df['Кіль-ть видач'].astype(int),
            bonus_col_name: final_df['Бонусы_суммарно'].astype(int),
            'ПІБ директора': final_df['ПІБ'].fillna('—'),
            'Таб.  номер': final_df['Табельный номер'].fillna('—')
        })
        
        # Добавление строки "Загалом"
        total_row = pd.DataFrame([{
            '№': None,
            'Магазин': 'Загалом',
            'Відділення': None,
            'Кіль-ть видач': final_report['Кіль-ть видач'].sum(),
            bonus_col_name: final_report[bonus_col_name].sum(),
            'ПІБ директора': None,
            'Таб.  номер': None
        }])
        
        final_report_with_total = pd.concat([final_report, total_row], ignore_index=True)
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            final_report_with_total.to_excel(writer, index=False, sheet_name=f'MeestБонуси{ukr_months_upper[prev_month_idx]}')
        
        st.success("Отчет сформирован 1-в-1 с образцом!")
        st.dataframe(final_report_with_total)
        
        st.download_button(
            label=f"📥 Скачать {output_filename}",
            data=output.getvalue(),
            file_name=output_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )