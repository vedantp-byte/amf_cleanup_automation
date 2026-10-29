import pandas as pd
import pdfplumber
import re

def extract_excel_bom(excel_path):
    """Dynamically parses the XLSX, avoiding row-length crashes and catching every ID."""
    expected_counts = {}
    
    # Read the raw excel file without header assumptions to prevent pandas from skipping data
    df_raw = pd.read_excel(excel_path, header=None).fillna("")
    
    headers = []
    item_idx = -1
    qty_idx = -1
    
    # Iterate through the raw rows
    for _, row in df_raw.iterrows():
        # Convert the row into a clean list of strings
        row_vals = [str(val).strip() for val in row.values]
        
        # Dynamically locate the header row
        if not headers and "Item ID" in row_vals:
            headers = row_vals
            item_idx = headers.index("Item ID")
            if "Final quantity" in headers:
                qty_idx = headers.index("Final quantity")
            continue
            
        # Once headers are locked in, process the actual data
        if headers and len(row_vals) > item_idx and item_idx != -1:
            raw_id = row_vals[item_idx]
            
            # Skip empty rows or repeated headers
            if not raw_id or raw_id.lower() == 'item id':
                continue
                
            # STRICT CLEANING: Remove spaces and force uppercase
            item_id = raw_id.upper()
            
            # Safely extract the expected quantity
            qty_str = row_vals[qty_idx] if qty_idx != -1 and len(row_vals) > qty_idx else "0"
            try:
                qty = int(float(qty_str))
            except ValueError:
                qty = 0
                
            # Add to our dictionary
            expected_counts[item_id] = expected_counts.get(item_id, 0) + qty
            
    return expected_counts

def extract_pdf_bom(pdf_path):
    """Scans raw PDF text and strictly COUNTS the occurrences of each Item ID."""
    actual_counts = {}
    
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=2, y_tolerance=2)
            if not text:
                continue
                
            for line in text.split('\n'):
                line = line.strip()
                if not line or "Filename" in line:
                    continue
                    
                # Regex targets 4+ alphanumeric chars followed strictly by .asm or .par
                match = re.search(r'([A-Z0-9\-]{4,})\.(?:asm|par|ASM|PAR)', line, re.IGNORECASE)
                
                if match:
                    # STRICT CLEANING: Force uppercase to match Excel
                    item_id = match.group(1).upper()
                    
                    # STRICT COUNTING: Just add 1 for every time we see this item on a line
                    actual_counts[item_id] = actual_counts.get(item_id, 0) + 1
                    
    return actual_counts

def generate_reconciliation_report(pdf_path, excel_path):
    print("Extracting Excel data...")
    expected_data = extract_excel_bom(excel_path)
    
    print("Extracting PDF data...")
    actual_data = extract_pdf_bom(pdf_path)
    
    print("Reconciling datasets...")
    # Combine all unique IDs from both the Excel and the PDF
    all_ids = set(expected_data.keys()).union(set(actual_data.keys()))
    
    report_rows = []
    
    for item_id in sorted(list(all_ids)):
        expected_qty = expected_data.get(item_id, 0)
        actual_qty = actual_data.get(item_id, 0)
        missing_qty = expected_qty - actual_qty
        
        # Strict conditional checks for status
        if actual_qty == 0 and expected_qty > 0:
            status = "Not in PDF"
        elif expected_qty == 0 and actual_qty > 0:
            status = "Not in Excel"
        elif expected_qty == actual_qty:
            status = "Match"
        else:
            status = "Mismatch"
            
        report_rows.append({
            'Item No': item_id,
            'Expected Quantity(Excel)': expected_qty,
            'Actual Quantity(PDF)': actual_qty,
            'Status': status,
            'Total Quantity Missing': missing_qty
        })
        
    # Generate final DataFrame
    df_report = pd.DataFrame(report_rows)
    
    # --- INSERT LEGEND STARTING ON ROW 4 ---
    df_report[''] = "" 
    df_report[' '] = "" 
    
    legend_lines = [
        "--- LEGEND: UNDERSTANDING TOTAL QUANTITY MISSING ---",
        "Positive (+) : Missing in PDF (Excel has it, but PDF is short)",
        "Negative (-) : Missing in Excel (PDF has it, but Excel is short)"
    ]
    
    for i, line in enumerate(legend_lines):
        row_idx = i + 2 
        if row_idx >= len(df_report):
            df_report.loc[row_idx] = {col: "" for col in df_report.columns}
            
        df_report.loc[row_idx, ' '] = line

    # --- THE DYNAMIC NAMING PATCH ---
    # Takes "1.xlsx", cuts at the dot, and turns it into "1_output.xlsx"
    base_name = excel_path.rsplit('.', 1)[0]
    output_path = f"{base_name}_output.xlsx"

    # Save directly as a native Excel (.xlsx) file
    df_report.to_excel(output_path, index=False)
    print(f"✅ Report generated successfully: {output_path}")# --- Execution ---
if __name__ == "__main__":

    PDF_FILE = input("Enter the path to the PDF file: ")  # Prompt user for PDF file path
    EXCEL_FILE = input("Enter the path to the Excel file: ")  # Prompt user for Excel file path
    
    generate_reconciliation_report(PDF_FILE, EXCEL_FILE)