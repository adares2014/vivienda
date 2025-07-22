import os
import json
import streamlit as st
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from datetime import datetime, timedelta
from openai import AzureOpenAI
import pandas as pd
from docx import Document
from io import BytesIO
import io

# === Configuración inicial ===
st.set_page_config(page_title="Analista Documental - Mejoramiento de Vivienda", layout="wide")

# === Cargar variables de entorno ===
load_dotenv()
AZURE_CONNECTION_STRING = os.getenv("AZURE_CONNECTION_STRING")
AZURE_CONTAINER_NAME = os.getenv("AZURE_CONTAINER_NAME")
AZURE_OPENAI_KEY = os.getenv("OPENAI_API_KEY_AZURE")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_GPT_DEPLOYMENT")
AZURE_OPENAI_API_VERSION = os.getenv("OPENAI_API_VERSION")

# === Inicialización de sesión ===
if 'documentos_contexto' not in st.session_state:
    st.session_state.documentos_contexto = ""
if 'chat_history' not in st.session_state:
    st.session_state.chat_history = []

# === Conexión al contenedor Azure Blob ===
@st.cache_resource
def get_blob_service_client():
    return BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)

try:
    blob_service_client = get_blob_service_client()
    container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)
except Exception as e:
    st.error(f"Error al conectar con Azure Blob Storage: {e}")
    st.stop()

# === Configurar cliente Azure OpenAI ===
@st.cache_resource
def get_openai_client():
    return AzureOpenAI(
        api_key=AZURE_OPENAI_KEY,
        api_version=AZURE_OPENAI_API_VERSION,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
    )

try:
    client = get_openai_client()
except Exception as e:
    st.error(f"Error al conectar con Azure OpenAI: {e}")
    st.stop()

# === Prompt base del sistema ===
prompt_base = """Eres un analista documental experto en proyectos de mejoramiento de vivienda. 
Respondes con base en los documentos del municipio (Excel, PDF, Word, imágenes) sincronizados desde OneDrive.
Si te preguntan por fotos de una cédula, genera enlaces SAS válidos por 90 días.
Si te preguntan por requisitos o faltantes, responde usando el archivo 'Estado_documental_postulados.xlsx'.
Usa la nemotecnia y reglas ubicadas en 'DOCUMENTOS_GENERALES_PROYECTO_DE_BUENAVENTURA'.
"""

# === Generar URL SAS ===
def generar_url_sas(blob_name):
    sas_token = generate_blob_sas(
        account_name=blob_service_client.account_name,
        container_name=AZURE_CONTAINER_NAME,
        blob_name=blob_name,
        account_key=blob_service_client.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(days=90)
    )
    return f"https://{blob_service_client.account_name}.blob.core.windows.net/{AZURE_CONTAINER_NAME}/{blob_name}?{sas_token}"

# === Leer archivos del contenedor ===
@st.cache_data(ttl=3600)  # Cache por 1 hora
def leer_archivos_texto():
    contenido_total = ""
    try:
        for blob in container_client.list_blobs():
            if blob.name.endswith(".txt") or blob.name.endswith(".md"):
                data = container_client.download_blob(blob.name).readall().decode("utf-8", errors="ignore")
                contenido_total += f"\n---\nArchivo: {blob.name}\n{data}\n"
            elif blob.name.endswith(".docx") and "DOCUMENTOS_GENERALES" in blob.name:
                doc_bytes = container_client.download_blob(blob.name).readall()
                doc = Document(io.BytesIO(doc_bytes))
                texto = "\n".join([p.text for p in doc.paragraphs])
                contenido_total += f"\n---\nArchivo: {blob.name}\n{texto}\n"
            elif blob.name.endswith(".xlsx") and "Estado_documental_postulados" in blob.name:
                df_bytes = container_client.download_blob(blob.name).readall()
                with open("temp_excel.xlsx", "wb") as f:
                    f.write(df_bytes)
                df = pd.read_excel("temp_excel.xlsx")
                contenido_total += f"\n---\nResumen documental:\n{df.to_string(index=False)}\n"
                os.remove("temp_excel.xlsx")
    except Exception as e:
        st.error(f"Error al leer archivos del contenedor: {e}")
    return contenido_total

# === Detectar imágenes asociadas a cédula ===
def encontrar_imagenes_por_cedula(cedula, municipio="6-Buenaventura-2025"):
    try:
        blob_name = f"{municipio}/urls_imagenes.json"
        blob_data = container_client.download_blob(blob_name).readall()
        index = json.loads(blob_data.decode("utf-8"))
        return index.get(cedula, [])
    except Exception as e:
        st.error(f"Error al leer índice de imágenes: {e}")
        return []

# === Cargar documentos al inicio ===
if not st.session_state.documentos_contexto:
    with st.spinner("Cargando documentos de contexto..."):
        st.session_state.documentos_contexto = leer_archivos_texto()

# === Interfaz de usuario ===
st.title("💬 Analista Documental - Mejoramiento de Vivienda")
st.markdown("""
Este asistente responde preguntas sobre proyectos de mejoramiento de vivienda en Buenaventura, 
basado en los documentos almacenados en Azure Blob Storage.
""")

# Sidebar con información
with st.sidebar:
    st.header("Configuración")
    st.info("Conectado a Azure Blob Storage y OpenAI")
    if st.button("Actualizar documentos de contexto"):
        with st.spinner("Actualizando documentos..."):
            st.session_state.documentos_contexto = leer_archivos_texto()
        st.success("Documentos actualizados!")

# Historial de chat
for message in st.session_state.chat_history:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if "images" in message and message["images"]:
            for img_url in message["images"]:
                st.image(img_url, caption="Documento asociado", width=300)

# Entrada del usuario
if prompt := st.chat_input("Escribe tu pregunta..."):
    # Mostrar pregunta del usuario
    with st.chat_message("user"):
        st.markdown(prompt)
    
    st.session_state.chat_history.append({"role": "user", "content": prompt})
    
    # Procesar pregunta
    with st.spinner("Buscando información..."):
        # Extraer cédulas de la pregunta
        cedulas_en_pregunta = [s for s in prompt.split() if s.isdigit() and len(s) >= 6]
        urls_imagenes = []
        
        # Buscar imágenes asociadas a cédulas
        for cedula in cedulas_en_pregunta:
            urls = encontrar_imagenes_por_cedula(cedula)
            if urls:
                urls_imagenes.extend([generar_url_sas(url) for url in urls])
        
        # Construir contexto
        contexto = prompt_base + st.session_state.documentos_contexto
        if urls_imagenes:
            contexto += "\n\nEnlaces de imágenes asociadas:\n" + "\n".join(urls_imagenes)
        
        # Consultar a OpenAI
        try:
            response = client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": contexto},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2
            )
            respuesta = response.choices[0].message.content
            
            # Mostrar respuesta
            with st.chat_message("assistant"):
                st.markdown(respuesta)
                if urls_imagenes:
                    for img_url in urls_imagenes:
                        st.image(img_url, caption="Documento asociado", width=300)
            
            # Guardar en historial
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": respuesta,
                "images": urls_imagenes if urls_imagenes else None
            })
            
        except Exception as e:
            st.error(f"Error al consultar OpenAI: {e}")