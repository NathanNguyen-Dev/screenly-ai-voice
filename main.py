from supabase import create_client, Client
import uuid  # for generating a new call_logs.id if needed
from datetime import datetime
import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from pydantic import BaseModel, Field # Import Pydantic

from dotenv import load_dotenv

# Debug imports
import sys
print(f"Python version: {sys.version}")
print(f"websockets path: {websockets.__file__}")
print(f"websockets version: {websockets.__version__}")

# Load the .env file
load_dotenv()

# Set up constants
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PUBLIC_SERVER_URL = os.getenv('PUBLIC_SERVER_URL')
PORT = int(os.getenv('PORT', 8080))
VOICE = 'coral'
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# Retrieve Supabase URL and Key from your environment
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Create the Supabase client instance
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# Initialize FastAPI application instance
app = FastAPI()

# Add CORS middleware
# TODO: Restrict origins for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows all origins
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods
    allow_headers=["*"], # Allows all headers
)

# Check for OpenAI API key
if not OPENAI_API_KEY:
    raise ValueError('MISSING OPENAI API KEY!')
if not PUBLIC_SERVER_URL:
    raise ValueError('MISSING PUBLIC_SERVER_URL environment variable!')

# --- Pydantic Models ---
class MakeCallRequest(BaseModel):
    candidate_id: uuid.UUID = Field(..., description="The UUID of the candidate to call")

# --- Helper Function for Prompt ---
def create_dynamic_prompt(candidate_name: str, job_title: str, job_questions: list[str]) -> str:
    prompt = f"Hey {candidate_name or 'there'}, I'm your AI interviewer for the {job_title} position—great to connect with you! "
    prompt += "Before we dive into the specifics, how's your day been so far? Feel free to take a moment to gather your thoughts. "
    prompt += "Here's our plan: "
    prompt += "First, tell me your story—what inspired you to pursue this field and apply for this role? "
    prompt += "Next, share the key skills and experiences you bring that align with the job description. "
    
    if job_questions:
        prompt += "Then, I have a few specific questions related to the role: "
        for i, q_text in enumerate(job_questions):
            prompt += f"Question {i+1}: {q_text} "
    else:
        prompt += "Then, we can discuss what excites you most about this opportunity. "

    prompt += "I'll follow up on your answers, ensuring we stay focused on the position. If you stray off-topic, I'll gently guide us back by asking, 'Interesting, how does that connect back to the role or your experience?' "
    prompt += "Remember to speak clearly—I'm here to make this a comfortable and engaging conversation. Let's get started!"
    return prompt

# --- API Endpoints ---

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio AI Interviewer is working!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    websocket_url = f"{PUBLIC_SERVER_URL.replace('https', 'wss')}/media-stream"
    print(f"Connecting Twilio Stream to: {websocket_url}")
    connect = Connect()
    connect.stream(url=websocket_url)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


# Handles the outbound call based on candidate_id
@app.post("/make-call")
async def make_outbound_call(call_request: MakeCallRequest):
    candidate_id = call_request.candidate_id
    print(f"Attempting to make call to candidate_id: {candidate_id}")
    try:
        # === Step 1: Fetch Candidate, Job, and Questions Data ===
        # Fetch candidate name, phone, and associated job_id
        candidate_response = supabase.table("candidates")\
            .select("id, full_name, phone_number, job_id")\
            .eq("id", str(candidate_id))\
            .limit(1)\
            .execute()

        candidate = candidate_response.data[0] if candidate_response.data else None
        if not candidate:
            print(f"Candidate with ID {candidate_id} not found.")
            return JSONResponse({"error": f"Candidate {candidate_id} not found"}, status_code=404)

        phone_number = candidate.get("phone_number")
        job_id = candidate.get("job_id")
        candidate_name = candidate.get("full_name", "Candidate") # Use name or default

        if not phone_number:
            print(f"Candidate {candidate_id} has no phone number.")
            return JSONResponse({"error": f"Candidate {candidate_id} no phone"}, status_code=400)
        if not job_id:
             print(f"Candidate {candidate_id} is not associated with a job.")
             return JSONResponse({"error": f"Candidate {candidate_id} has no job"}, status_code=400)

        # Fetch job title
        job_response = supabase.table("jobs")\
            .select("title")\
            .eq("id", str(job_id))\
            .limit(1)\
            .execute()
        
        job = job_response.data[0] if job_response.data else None
        if not job:
             print(f"Job with ID {job_id} not found for candidate {candidate_id}.")
             return JSONResponse({"error": f"Job {job_id} not found"}, status_code=404)
        job_title = job.get("title", "position") # Use title or default

        # Fetch job questions
        questions_response = supabase.table("job_questions")\
            .select("question_text")\
            .eq("job_id", str(job_id))\
            .order("created_at")\
            .execute()
        
        job_questions = [q['question_text'] for q in questions_response.data] if questions_response.data else []
        print(f"Found {len(job_questions)} questions for job {job_id}.")

        # === Step 2: Construct Dynamic Prompt ===
        dynamic_system_prompt = create_dynamic_prompt(candidate_name, job_title, job_questions)
        print(f"Generated Prompt:\n{dynamic_system_prompt}") # Log generated prompt for debugging

        # === Step 3: Initiate Twilio Call ===
        twilio_callback_url = f"{PUBLIC_SERVER_URL}/incoming-call"
        print(f"Setting Twilio callback URL to: {twilio_callback_url}")
        call = client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=twilio_callback_url
        )
        call_sid = call.sid # Get the Call SID from Twilio
        print(f"Twilio call initiated with SID: {call_sid}")

        # === Step 4: Log Call with SID and Prompt ===
        log_entry = {
            "id": str(uuid.uuid4()),
            "candidate_id": str(candidate_id),
            "status": "pending",
            "started_at": datetime.now().isoformat(),
            "call_sid": call_sid, # Store the Twilio Call SID
            "system_prompt": dynamic_system_prompt # Store the generated prompt
        }
        insert_response = supabase.table("call_logs").insert(log_entry).execute()
        
        # Optional: Check insert_response for errors if needed
        # if insert_response.error:
        #    print(f"Error logging call: {insert_response.error}")
        #    # Decide how to handle logging failure - proceed with call anyway?

        # === Step 5: Return Response ===
        return JSONResponse({
            "status": "calling",
            "candidate_id": str(candidate_id),
            "call_sid": call_sid
        })

    except Exception as e:
        print(f"Error during make_outbound_call for candidate {candidate_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("Client connected")
    await websocket.accept()

    openai_ws = None # Define openai_ws here to ensure it's accessible in finally block
    system_prompt_to_use = None # Store fetched prompt
    call_sid = None # Store call_sid

    try:
        # Need to get call_sid first from the 'start' event before connecting to OpenAI
        # Wait for the start event from Twilio
        start_message = await websocket.receive_text()
        start_data = json.loads(start_message)
        
        if start_data.get('event') == 'start':
            stream_sid = start_data['start']['streamSid']
            call_sid = start_data['start']['callSid'] # Get the Call SID
            print(f"Incoming stream started: {stream_sid}, Call SID: {call_sid}")
            
            # --- Fetch System Prompt using Call SID ---
            try:
                response = supabase.table("call_logs")\
                    .select("system_prompt")\
                    .eq("call_sid", call_sid)\
                    .limit(1)\
                    .maybe_single()\
                    .execute()
                
                if response.data and response.data.get('system_prompt'):
                    system_prompt_to_use = response.data['system_prompt']
                    print(f"Found system prompt for Call SID {call_sid}")
                else:
                    print(f"WARNING: System prompt not found for Call SID {call_sid}. Using default.")
                    # Decide on fallback behavior: use default, raise error, etc.
                    # system_prompt_to_use = SYSTEM_MESSAGE # Option: fallback to default
                    raise ValueError(f"System prompt not found for Call SID {call_sid}") # Option: raise error
                    
            except Exception as db_exc:
                print(f"ERROR fetching system prompt for Call SID {call_sid}: {db_exc}")
                # Decide how to handle DB error - maybe disconnect?
                await websocket.close(code=1011, reason="Database error fetching prompt")
                return # Exit if we can't get the prompt
            # --- End Fetch System Prompt ---
        else:
             print(f"ERROR: Expected 'start' event first, but received: {start_data.get('event')}")
             await websocket.close(code=1002, reason="Protocol error: Expected start event")
             return # Exit if protocol is wrong

        # Now connect to OpenAI with the fetched prompt
        url = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01'
        headers = [
            ("Authorization", f"Bearer {OPENAI_API_KEY}"),
            ("OpenAI-Beta", "realtime=v1")
        ]

        async with websockets.connect(url, extra_headers=headers) as openai_ws_conn:
            openai_ws = openai_ws_conn # Assign to outer scope variable
            print("Connected to OpenAI WebSocket")
            await send_session_update(openai_ws, system_prompt_to_use) # Pass the dynamic prompt
            # stream_sid is already set from the start event

            async def receive_from_twilio():
                try:
                    # We already processed the start message, now listen for media/stop
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        print(f"Twilio event: {data['event']}")
                        if data['event'] == 'media' and openai_ws and openai_ws.open:
                            print(
                                f"Sending audio to OpenAI: {len(data['media']['payload'])} bytes")
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            await openai_ws.send(json.dumps(audio_append))
                        elif data['event'] == 'stop':
                            print(f"Twilio stream stopped for Call SID {call_sid}")
                            if openai_ws and openai_ws.open:
                                print("Closing OpenAI WebSocket gracefully.")
                                await openai_ws.close()
                            break # Exit receive loop on stop

                except WebSocketDisconnect:
                    print("Twilio WebSocket disconnected unexpectedly.")
                    # Ensure OpenAI connection is closed if Twilio disconnects abruptly
                    if openai_ws and openai_ws.open:
                        print("Closing OpenAI WebSocket due to Twilio disconnect.")
                        await openai_ws.close()

            async def send_to_twilio():
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        print(f"OpenAI event: {response['type']}")
                        if response['type'] == 'session.updated':
                            print("Session updated successfully:", response)
                        elif response['type'] == 'response.audio.delta' and response.get('delta'):
                            audio_payload = base64.b64encode(
                                base64.b64decode(response['delta'])).decode('utf-8')
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload}
                            }
                            # Check if Twilio websocket is still open before sending
                            if websocket.client_state == websockets.protocol.State.OPEN:
                                await websocket.send_json(audio_delta)
                            else:
                                print("Twilio WebSocket closed, cannot send audio delta.")
                                # Optionally break here if Twilio is gone
                        # Handle other OpenAI messages if needed (e.g., transcription, errors)

                except websockets.ConnectionClosed as e:
                    print(f"OpenAI WebSocket closed: {e}")
                    # Ensure Twilio connection is closed if OpenAI closes
                    if websocket.client_state == websockets.protocol.State.OPEN:
                        print("Closing Twilio WebSocket due to OpenAI closure.")
                        await websocket.close()
                except Exception as e:
                    print(f"Error in send_to_twilio: {e}")
                    # Consider closing connections on error
                    if openai_ws and openai_ws.open:
                        await openai_ws.close()
                    if websocket.client_state == websockets.protocol.State.OPEN:
                        await websocket.close()

            # Run receiver and sender concurrently
            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except websockets.exceptions.InvalidHandshake as e:
        print(f"Failed WebSocket handshake with OpenAI: {e}")
        # Ensure Twilio connection is closed
        if websocket.client_state == websockets.protocol.State.OPEN:
            await websocket.close(code=1011, reason="OpenAI handshake failed")
    except Exception as e:
        print(f"Error in handle_media_stream: {e}")
        # Ensure connections are closed on general errors
        if openai_ws and openai_ws.open:
            try: await openai_ws.close() 
            except: pass # Ignore errors during cleanup
        if websocket.client_state == websockets.protocol.State.OPEN:
            try: await websocket.close(code=1011, reason="Server error") 
            except: pass # Ignore errors during cleanup
    finally:
        print(f"Media stream handler finished for Call SID {call_sid}")
        # Final check to ensure connections are closed 
        if openai_ws and openai_ws.open:
            try: await openai_ws.close() 
            except: pass
        if websocket.client_state == websockets.protocol.State.OPEN:
            try: await websocket.close() 
            except: pass


async def send_session_update(openai_ws, system_prompt: str): # Accept prompt argument
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 300},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": system_prompt, # Use the dynamic prompt
            "modalities": ["text", "audio"],
            "temperature": 0.7,
        }
    }
    print('Sending session update with dynamic prompt...')
    await openai_ws.send(json.dumps(session_update))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
