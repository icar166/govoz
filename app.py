import os
import io
import json
import sqlite3
import httpx
from datetime import datetime, timedelta
from typing import List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ReportLab para la generación del PDF profesional
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

app = FastAPI(title="Govoz API", version="1.1.0")

# Permitir solicitudes desde el frontend PWA (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inicializar cliente de Gemini utilizando la variable de entorno
API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=API_KEY) if API_KEY else None

# Configuración de NOWPayments (Credenciales desde Secrets de Hugging Face)
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_API_URL = "https://api.nowpayments.io/v1/payment"

# --- CONFIGURACIÓN DE BASE DE DATOS SQLITE (Suscripciones) ---
def init_db():
    conn = sqlite3.connect("govoz_subscriptions.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            rif TEXT PRIMARY KEY,
            nombre_negocio TEXT,
            expiration_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def verificar_suscripcion(rif: str) -> bool:
    """Verifica si el RIF tiene una suscripción activa con fecha vigente."""
    conn = sqlite3.connect("govoz_subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("SELECT expiration_date FROM subscriptions WHERE rif = ?", (rif,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return False
    
    exp_date_str = row[0]
    exp_date = datetime.fromisoformat(exp_date_str)
    return datetime.now() < exp_date


# --- ESQUEMAS DE IA (Structured Outputs) ---
class ItemSchema(BaseModel):
    cantidad: float = Field(description="Cantidad del producto dictada por voz")
    descripcion: str = Field(description="Nombre claro del producto o servicio")
    precio_unitario: float = Field(description="Precio unitario por cada producto")

class NotaEntregaExtraccion(BaseModel):
    cliente_nombre: str = Field(description="Nombre del cliente o 'Cliente General' si no se menciona")
    cliente_telefono: str = Field(description="Teléfono del cliente o 'N/A' si no se menciona")
    iva_porcentaje: float = Field(description="Porcentaje de IVA aplicable, por defecto 16.0")
    items: List[ItemSchema] = Field(description="Lista de productos extraídos del audio")


@app.get("/")
def home():
    return {"status": "ok", "app": "Govoz Backend", "engine": "Gemini 2.5 Flash + NOWPayments Polygon"}


# --- ENDPOINT 1: PROCESAR AUDIO Y GENERAR PDF ---
@app.post("/procesar-nota-entrega")
async def procesar_nota_entrega(
    audio: UploadFile = File(...),
    negocio_nombre: str = Form("MI COMERCIO"),
    negocio_rif: str = Form("J-00000000-0"),
    negocio_telefono: str = Form("N/A"),
    negocio_direccion: str = Form("Venezuela"),
):
    if not verificar_suscripcion(negocio_rif):
        raise HTTPException(
            status_code=402, 
            detail="Suscripción vencida o inactiva. Por favor renueva tu membresía mensual para continuar usando Govoz."
        )

    if not client:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY no está configurada en los Secrets.")

    try:
        audio_bytes = await audio.read()
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=audio.content_type or 'audio/ogg'),
                """
                Escucha este audio de un comerciante en Venezuela. Extrae los productos, 
                cantidades, precios y datos del cliente para una nota de entrega.
                Si no mencionan el cliente, asigna 'Cliente General'. Si no mencionan teléfono, asigna 'N/A'.
                """
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=NotaEntregaExtraccion,
            ),
        )

        datos_ia = json.loads(response.text)

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
        elements = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('StoreTitle', parent=styles['Heading1'], fontSize=16, leading=20, alignment=1, textColor=colors.HexColor("#1A202C"))
        subtitle_style = ParagraphStyle('StoreSub', parent=styles['Normal'], fontSize=10, leading=14, alignment=1, textColor=colors.HexColor("#4A5568"))
        header_box_style = ParagraphStyle('HeaderBox', parent=styles['Normal'], fontSize=11, leading=15, alignment=1, textColor=colors.HexColor("#2B6CB0"))

        elements.append(Paragraph(f"<b>{negocio_nombre}</b>", title_style))
        elements.append(Paragraph(f"RIF: {negocio_rif} | Teléfono: {negocio_telefono}", subtitle_style))
        elements.append(Paragraph(f"Dirección: {negocio_direccion}", subtitle_style))
        elements.append(Spacer(1, 15))

        elements.append(Paragraph("<b>NOTA DE ENTREGA</b>", header_box_style))
        elements.append(Paragraph("<b>N° NE-2026-0001</b>", subtitle_style))
        elements.append(Spacer(1, 10))

        cliente_nom = datos_ia.get('cliente_nombre') or 'Cliente General'
        cliente_tel = datos_ia.get('cliente_telefono') or 'N/A'
        client_info = f"<b>Cliente:</b> {cliente_nom}<br/><b>Teléfono:</b> {cliente_tel}"
        elements.append(Paragraph(client_info, styles['Normal']))
        elements.append(Spacer(1, 10))

        table_data = [["Cant.", "Descripción", "P. Unit", "Total"]]
        subtotal = 0.0
        iva_porcentaje = float(datos_ia.get('iva_porcentaje', 16.0))

        for item in datos_ia.get('items', []):
            cant = float(item.get('cantidad', 1.0))
            p_unit = float(item.get('precio_unitario', 0.0))
            total_item = cant * p_unit
            subtotal += total_item
            table_data.append([
                str(cant),
                item.get('descripcion', 'Producto sin descripción'),
                f"${p_unit:.2f}",
                f"${total_item:.2f}"
            ])

        iva_monto = subtotal * (iva_porcentaje / 100.0)
        total_general = subtotal + iva_monto

        table_data.append(["", "", "Subtotal:", f"${subtotal:.2f}"])
        table_data.append(["", "", f"IVA ({iva_porcentaje}%):", f"${iva_monto:.2f}"])
        table_data.append(["", "", "TOTAL:", f"${total_general:.2f}"])

        t = Table(table_data, colWidths=[50, 270, 80, 80])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#EDF2F7")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor("#2D3748")),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
            ('GRID', (0, 0), (-1, -5), 0.5, colors.HexColor("#CBD5E0")),
            ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
            ('LINEABOVE', (2, -3), (3, -3), 1, colors.HexColor("#4A5568")),
            ('FONTNAME', (2, -1), (3, -1), 'Helvetica-Bold'),
        ]))
        
        elements.append(t)
        elements.append(Spacer(1, 20))

        footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, alignment=1, textColor=colors.HexColor("#718096"))
        elements.append(Paragraph("Documento de uso interno / Nota de Entrega no fiscal", footer_style))
        elements.append(Paragraph("¡Gracias por su compra! — Generado con Govoz", footer_style))

        doc.build(elements)
        buffer.seek(0)

        return Response(
            content=buffer.getvalue(),
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=nota_entrega.pdf"}
        )

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT 2: CREAR ENLACE DE PAGO CON NOWPAYMENTS ($5 USDT Polygon) ---
@app.post("/crear-pago-suscripcion")
async def crear_pago_suscripcion(rif: str = Form(...), nombre_negocio: str = Form(...)):
    if not NOWPAYMENTS_API_KEY:
        raise HTTPException(status_code=500, detail="NOWPAYMENTS_API_KEY no está configurada.")

    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "price_amount": 5.0,
        "price_currency": "usd",
        "pay_currency": "usdtpolygon",
        "order_id": rif,
        "order_description": f"Suscripción mensual Govoz para {nombre_negocio} (RIF: {rif})",
        "ipn_callback_url": "https://tu-space.hf.space/webhook/nowpayments"
    }

    async with httpx.AsyncClient() as client_http:
        try:
            response = await client_http.post(NOWPAYMENTS_API_URL, json=payload, headers=headers)
            data = response.json()
            if response.status_code not in [200, 201]:
                raise HTTPException(status_code=400, detail=f"Error en NOWPayments: {data}")
            return {"status": "success", "pago_info": data}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT 3: WEBHOOK IPN DE NOWPAYMENTS ---
@app.post("/webhook/nowpayments")
async def nowpayments_webhook(request: Request):
    data = await request.json()
    payment_status = data.get("payment_status")
    rif = data.get("order_id")

    if payment_status == "finished" and rif:
        conn = sqlite3.connect("govoz_subscriptions.db")
        cursor = conn.cursor()
        
        cursor.execute("SELECT expiration_date FROM subscriptions WHERE rif = ?", (rif,))
        row = cursor.fetchone()
        
        now = datetime.now()
        if row:
            current_exp = datetime.fromisoformat(row[0])
            base_date = current_exp if current_exp > now else now
            nueva_exp = base_date + timedelta(days=30)
            cursor.execute("UPDATE subscriptions SET expiration_date = ? WHERE rif = ?", (nueva_exp.isoformat(), rif))
        else:
            nueva_exp = now + timedelta(days=30)
            cursor.execute("INSERT INTO subscriptions (rif, nombre_negocio, expiration_date) VALUES (?, ?, ?)", 
                           (rif, "Comercio Govoz", nueva_exp.isoformat()))
            
        conn.commit()
        conn.close()
        return {"status": "suscripcion_actualizada", "rif": rif, "nueva_expiracion": nueva_exp.isoformat()}

    return {"status": "pendiente_o_ignorado"}

# --- ARRANQUE DE UVICORN PARA GRADIO SPACES ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7860)