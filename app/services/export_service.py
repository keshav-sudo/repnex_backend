import io
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

def generate_excel(title: str, headers: list[str], rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    
    # Enable grid lines
    ws.views.sheetView[0].showGridLines = True

    # Style definitions
    title_font = Font(name="Calibri", size=16, bold=True, color="1B365D")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
    
    data_font = Font(name="Calibri", size=11)
    zebra_fill = PatternFill(start_color="F7F9FC", end_color="F7F9FC", fill_type="solid")
    
    thin_side = Side(border_style="thin", color="E0E0E0")
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    
    # Title
    ws.append([title])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(headers), 1))
    title_cell = ws.cell(row=1, column=1)
    title_cell.font = title_font
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 40
    
    # Empty row
    ws.append([])
    ws.row_dimensions[2].height = 15

    # Headers
    ws.append(headers)
    header_row_idx = 3
    ws.row_dimensions[header_row_idx].height = 28
    
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=header_row_idx, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left", vertical="center")
        cell.border = cell_border
        
    # Data Rows
    for r_idx, row in enumerate(rows, start=4):
        row_values = [row.get(h, "") for h in headers]
        ws.append(row_values)
        ws.row_dimensions[r_idx].height = 20
        
        is_even = (r_idx % 2 == 0)
        for c_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=r_idx, column=c_idx)
            cell.font = data_font
            cell.border = cell_border
            if is_even:
                cell.fill = zebra_fill
            
            val = cell.value
            if isinstance(val, (int, float)):
                cell.alignment = Alignment(horizontal="right", vertical="center")
                if isinstance(val, float):
                    cell.number_format = '0.00'
                else:
                    cell.number_format = '#,##0'
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
                
    # Auto-adjust column widths
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col[2:]:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

def generate_pdf(title: str, headers: list[str], rows: list[dict]) -> bytes:
    # Use landscape letter if there are more than 6 columns, else portrait
    page_size = landscape(letter) if len(headers) > 6 else letter
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'ReportTitle',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#1B365D'),
        spaceAfter=15
    )
    
    header_style = ParagraphStyle(
        'TableHeader',
        fontSize=9,
        leading=11,
        fontName='Helvetica-Bold',
        textColor=colors.white
    )
    
    body_style = ParagraphStyle(
        'TableBody',
        fontSize=8,
        leading=10,
        fontName='Helvetica',
        textColor=colors.HexColor('#333333')
    )
    
    elements = []
    elements.append(Paragraph(title, title_style))
    elements.append(Spacer(1, 10))
    
    table_data = []
    header_row = [Paragraph(h, header_style) for h in headers]
    table_data.append(header_row)
    
    for row in rows:
        row_cells = []
        for h in headers:
            val = row.get(h, "")
            if val is None:
                val_str = ""
            elif isinstance(val, float):
                val_str = f"{val:.2f}"
            else:
                val_str = str(val)
            row_cells.append(Paragraph(val_str, body_style))
        table_data.append(row_cells)
        
    doc_width = doc.width
    col_width = doc_width / len(headers)
    col_widths = [col_width] * len(headers)
    
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    
    t_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B365D')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E0E0E0')),
    ])
    
    for i in range(1, len(rows) + 1):
        if i % 2 == 0:
            t_style.add('BACKGROUND', (0, i), (-1, i), colors.HexColor('#F7F9FC'))
            
    t.setStyle(t_style)
    elements.append(t)
    
    doc.build(elements)
    return buffer.getvalue()
