from supabase import create_client, Client
import uuid  # for generating a new call_logs.id if needed
from datetime import datetime
import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.websockets import WebSocketDisconnect, WebSocketState
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Connect, Parameter
from twilio.rest import Client
from pydantic import BaseModel, Field # Import Pydantic
from collections import defaultdict # Added
from typing import Dict # Added

from dotenv import load_dotenv
import logging # Added

# Debug imports
import sys
print(f"Python version: {sys.version}")
print(f"websockets path: {websockets.__file__}")
print(f"websockets version: {websockets.__version__}")

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load the .env file
load_dotenv()

# Set up constants
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PUBLIC_SERVER_URL = os.getenv('PUBLIC_SERVER_URL')
PORT = int(os.getenv('PORT', 8080))
# Base system message - will be formatted per candidate/job
# Note: The initial greeting is now handled separately by an explicit message.
BASE_SYSTEM_MESSAGE = (
    "My instructions after delivering my initial greeting are: " # Clarify timing
    "Proceed with the interview plan for the {job_title} role: "
    "First, ask the candidate to tell their story—what inspired them to pursue this field and get into this role? "
    "Next, ask them to share the skills and experiences they bring to the team for the {job_title} role. Remind them to take their time. "
    "Finally, ask them what excites them most about this {job_title} position opportunity. "
    "Follow up on their answers naturally, ensuring the conversation stays focused on the position. If they stray off-topic, gently guide them back by asking, 'That's interesting, how does that relate to the {job_title} position?' "
    "Maintain a friendly and engaging tone. Remember to speak slowly and clearly."
)
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

# Global session store (in-memory) - Stores state per streamSid
# Structure: { stream_sid: { "openai_ws": websocket_connection, "prompt": str, "tasks": set } }
active_sessions: Dict[str, Dict] = {}

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

# Helper function to build dynamic prompt
def build_dynamic_prompt(candidate_name: str, job_title: str) -> str:
    """Builds the dynamic system prompt for OpenAI."""
    if not candidate_name: candidate_name = "there" # Fallback name
    if not job_title: job_title = "position" # Fallback title
    return BASE_SYSTEM_MESSAGE.format(candidate_name=candidate_name, job_title=job_title)

# --- Pydantic Models ---
class MakeCallRequest(BaseModel):
    candidate_id: uuid.UUID = Field(..., description="The UUID of the candidate to call")

# --- API Endpoints ---

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio AI Interviewer is working!"}

@app.api_route("/generate-outbound-twiml/{candidate_id}", methods=["GET", "POST"])
async def generate_outbound_twiml(request: Request, candidate_id: uuid.UUID):
    """Generates TwiML for the outbound call with dynamic parameters."""
    logger.info(f"Generating TwiML for candidate_id: {candidate_id}")
    try:
        # Fetch candidate name and job title from Supabase
        # TODO: Verify actual table/column names and relationships in Supabase schema
        # Assuming 'candidates' has 'full_name', 'job_id'
        # Assuming 'jobs' has 'id', 'title' and is linked from candidates.job_id
        response = supabase.table("candidates")\
            .select("full_name, jobs(title)")\
            .eq("id", str(candidate_id))\
            .maybe_single()\
            .execute()

        candidate_data = response.data
        if not candidate_data:
            logger.error(f"Candidate {candidate_id} not found for TwiML generation.")
            raise HTTPException(status_code=404, detail="Candidate not found")

        # Use full_name directly
        candidate_name = candidate_data.get("full_name", "Candidate").strip() or "Candidate" # Fallback

        job_data = candidate_data.get("jobs")
        job_title = job_data.get("title") if job_data else "the role" # Fallback

        logger.info(f"Found Candidate: {candidate_name}, Job: {job_title}")

        websocket_url = f"{PUBLIC_SERVER_URL.replace('http', 'ws', 1)}/twilio-stream"
        logger.info(f"Setting Stream URL for Twilio: {websocket_url}")

        response = VoiceResponse()
        connect = Connect()
        stream = connect.stream(url=websocket_url)
        # Pass dynamic parameters to the WebSocket handler
        stream.parameter(name="CandidateName", value=candidate_name)
        stream.parameter(name="JobTitle", value=job_title)
        response.append(connect)

        # TODO: Add security check for X-Twilio-Signature here if needed for GET requests

        return HTMLResponse(content=str(response), media_type="application/xml")

    except HTTPException as http_exc:
        raise http_exc # Re-raise FastAPI HTTP exceptions
    except Exception as e:
        logger.error(f"Error generating TwiML for candidate {candidate_id}: {e}", exc_info=True)
        # Return a generic error TwiML or raise HTTP 500
        response = VoiceResponse()
        response.say("Sorry, an error occurred while preparing the call. Please try again later.")
        return HTMLResponse(content=str(response), media_type="application/xml", status_code=500)

# Handles the outbound call based on candidate_id
@app.post("/make-call")
async def make_outbound_call(call_request: MakeCallRequest): # Accept request body
    candidate_id = call_request.candidate_id
    logger.info(f"Attempting to make call to candidate_id: {candidate_id}") # Use logger
    try:
        # ✅ Step 1: Retrieve the specified candidate record from Supabase.
        response = supabase.table("candidates")\
            .select("id, phone_number")\
            .eq("id", str(candidate_id))\
            .limit(1)\
            .execute()

        # Check if a candidate record was found.
        candidate = response.data[0] if response.data else None
        if not candidate:
            logger.error(f"Candidate {candidate_id} not found in Supabase.")
            return JSONResponse({"error": f"Candidate with ID {candidate_id} not found"}, status_code=404)

        # Extract the candidate's phone number (ID is already known)
        phone_number = candidate["phone_number"]
        if not phone_number:
            logger.error(f"Candidate {candidate_id} found but has no phone number.")
            return JSONResponse({"error": f"Candidate {candidate_id} has no phone number"}, status_code=400)

        # ✅ Step 2: Use Twilio to initiate call using the TwiML generation endpoint
        twilio_twiml_url = f"{PUBLIC_SERVER_URL}/generate-outbound-twiml/{candidate_id}"
        logger.info(f"Initiating Twilio call to {phone_number} using TwiML URL: {twilio_twiml_url}") # Correct URL log
        # TODO: Add status_callback handling if needed to track call progress externally
        call = client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=twilio_twiml_url # Corrected: Use the TwiML generation URL
        )

        # ✅ Step 3: Log the call attempt (status starts as 'pending')
        # Log using the correct 'call_sid' column name and an allowed status
        supabase.table("call_logs").insert({
            "id": str(uuid.uuid4()),
            "candidate_id": str(candidate_id),
            "status": "pending", # Reverted: Use 'pending' status to satisfy constraint
            "started_at": datetime.now().isoformat(),
            "call_sid": call.sid
        }).execute()

        # ✅ Step 4: Return a JSON response confirming the call and showing key details.
        return JSONResponse({
            "status": "calling",
            "candidate_id": str(candidate_id), # Return the ID that was called
            "call_sid": call.sid
        })

    except Exception as e:
        logger.error(f"Error during make_outbound_call for candidate {candidate_id}: {e}")
        # ❌ If any errors occur, catch them and return a clean error message.
        return JSONResponse({"error": str(e)}, status_code=500)

async def initialize_openai_session(openai_ws, session_id: str, dynamic_prompt: str):
    """Sends the session.update message to configure the OpenAI session."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 300},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": dynamic_prompt, 
            "modalities": ["text", "audio"],
            "temperature": 0.7, # Adjusted from example to keep previous value
        }
    }
    logger.info(f"[{session_id}] Preparing to send session update to OpenAI.")
    await openai_ws.send(json.dumps(session_update))
    logger.info(f"[{session_id}] Successfully sent session update to OpenAI.")

async def send_initial_greeting(openai_ws, session_id: str, candidate_name: str):
    """Sends the initial greeting item and triggers the response generation."""
    initial_greeting_text = f"Hello {candidate_name}, I am Screenly, your AI interviewer. Great to connect with you!"
    # Step 1: Create the conversation item
    initial_message = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "input_text",
                    "text": initial_greeting_text
                }
            ]
        }
    }
    logger.info(f"[{session_id}] Sending initial ASSISTANT greeting item to OpenAI.")
    await openai_ws.send(json.dumps(initial_message))

    # Step 2: Trigger the response generation
    trigger_response = {"type": "response.create"}
    logger.info(f"[{session_id}] Sending response.create trigger to OpenAI.")
    await openai_ws.send(json.dumps(trigger_response))

@app.websocket("/twilio-stream") # Renamed from /media-stream
async def handle_twilio_stream(websocket: WebSocket):
    """Handles a single Twilio Media Stream connection."""
    await websocket.accept()
    logger.info(f"Twilio WebSocket client connected: {websocket.client}")

    session_id = None
    openai_ws = None
    receive_task = None
    send_task = None

    try:
        # Establish connection to OpenAI for this session
        openai_url = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01'
        openai_headers = [
            ("Authorization", f"Bearer {OPENAI_API_KEY}"),
            ("OpenAI-Beta", "realtime=v1")
        ]
        logger.info("Attempting to connect to OpenAI WebSocket...")
        openai_ws = await websockets.connect(openai_url, extra_headers=openai_headers)
        logger.info("Connected to OpenAI WebSocket.")

        async def receive_from_twilio(twilio_ws, oai_ws):
            nonlocal session_id # Allow modification of outer scope variable
            try:
                async for message_text in twilio_ws.iter_text():
                    data = json.loads(message_text)
                    event = data.get("event")
                    # logger.debug(f"Received from Twilio: {event}") # Very verbose

                    if event == "start":
                        session_id = data['start']['streamSid']
                        call_sid = data['start']['callSid']
                        custom_params = data['start'].get('customParameters', {})
                        candidate_name = custom_params.get('CandidateName', 'there')
                        job_title = custom_params.get('JobTitle', 'the position')
                        logger.info(f"Stream started: SID={session_id}, CallSID={call_sid}, Candidate={candidate_name}, Job={job_title}")

                        # Build dynamic prompt and store session state
                        dynamic_prompt = build_dynamic_prompt(candidate_name, job_title)
                        active_sessions[session_id] = {
                            "openai_ws": oai_ws,
                            "prompt": dynamic_prompt,
                            "tasks": set() 
                        }

                        # Initialize session and send greeting using helper functions
                        await initialize_openai_session(oai_ws, session_id, dynamic_prompt)
                        await send_initial_greeting(oai_ws, session_id, candidate_name)

                    elif event == "media" and session_id and oai_ws and oai_ws.open:
                        audio_b64 = data['media']['payload']
                        # logger.debug(f"[{session_id}] Forwarding {len(audio_b64)} audio bytes Twilio -> OpenAI")
                        audio_event = {
                            "type": "input_audio_buffer.append",
                            "audio": audio_b64
                        }
                        await oai_ws.send(json.dumps(audio_event))

                    elif event == "stop":
                        logger.info(f"[{session_id}] Received stop event from Twilio.")
                        break # Exit the loop gracefully

                    elif event == "mark":
                        mark_name = data.get("mark", {}).get("name")
                        # logger.debug(f"[{session_id}] Received mark from Twilio: {mark_name}")
                        # Can be used for synchronization if needed

            except WebSocketDisconnect:
                logger.info(f"[{session_id or 'Unknown'}] Twilio WebSocket disconnected.")
            except websockets.ConnectionClosedOK:
                 logger.info(f"[{session_id or 'Unknown'}] Twilio connection closed normally.")
            except Exception as e:
                logger.error(f"[{session_id or 'Unknown'}] Error in receive_from_twilio: {e}", exc_info=True)
            finally:
                logger.info(f"[{session_id or 'Unknown'}] Exiting receive_from_twilio loop.")


        async def send_to_twilio(twilio_ws, oai_ws):
            nonlocal session_id # Access outer scope session_id
            try:
                logger.info(f"[{session_id or 'Unknown'}] send_to_twilio loop waiting for OpenAI message...")
                async for openai_message in oai_ws:
                    logger.info(f"[{session_id or 'Unknown'}] send_to_twilio loop received message from OpenAI.")
                    if not session_id: # Don't send if session not started
                        continue

                    # Explicitly log state before checking
                    current_twilio_state = twilio_ws.client_state
                    logger.info(f"[{session_id}] Checking Twilio WS state before sending. State: {current_twilio_state}")
                    if current_twilio_state != WebSocketState.CONNECTED:
                         logger.warning(f"[{session_id}] Twilio WS not connected ({current_twilio_state}), cannot send OpenAI message.")
                         break
                    
                    response = json.loads(openai_message)
                    response_type = response.get("type")
                    # logger.debug(f"[{session_id}] Received from OpenAI: {response_type}") # Very verbose

                    if response_type == 'session.updated':
                        logger.info(f"[{session_id}] OpenAI session updated successfully.")
                    elif response_type == 'response.audio.delta' and response.get('delta'):
                        audio_b64 = response['delta']
                        # logger.debug(f"[{session_id}] Forwarding {len(audio_b64)} audio bytes OpenAI -> Twilio")
                        twilio_media = {
                            "event": "media",
                            "streamSid": session_id,
                            "media": {"payload": audio_b64}
                        }
                        await twilio_ws.send_json(twilio_media)

                        # Send mark event for synchronization
                        mark_event = {
                            "event": "mark",
                            "streamSid": session_id,
                            "mark": {"name": f"openai_chunk_{uuid.uuid4()}"} # Unique mark name
                        }
                        await twilio_ws.send_json(mark_event)

                    elif response_type == 'response.text.delta':
                         # Optional: Log or process text transcription delta if needed
                         # logger.debug(f"[{session_id}] OpenAI Text Delta: {response.get('delta')}")
                         pass
                    elif response_type == 'conversation.item.updated':
                         # Optional: Log full conversation turns if needed
                         # logger.debug(f"[{session_id}] OpenAI Item Updated: {response.get('item')}")
                         pass
                    elif response_type == 'error':
                         logger.error(f"[{session_id}] OpenAI API Error: {response.get('error')}")
                         # Potentially close the connection or send an error message via Twilio

            except websockets.ConnectionClosed as e:
                logger.info(f"[{session_id or 'Unknown'}] OpenAI WebSocket closed: Code={e.code}, Reason='{e.reason}'")
            except Exception as e:
                logger.error(f"[{session_id or 'Unknown'}] Error in send_to_twilio: {e}", exc_info=True)
            finally:
                 logger.info(f"[{session_id or 'Unknown'}] Exiting send_to_twilio loop.")


        # Start the concurrent tasks for this session
        receive_task = asyncio.create_task(receive_from_twilio(websocket, openai_ws))
        send_task = asyncio.create_task(send_to_twilio(websocket, openai_ws))

        # Store task references in session for potential cancellation (though gather handles waiting)
        if session_id: # Should be set quickly by receive_from_twilio
             active_sessions[session_id]["tasks"].add(receive_task)
             active_sessions[session_id]["tasks"].add(send_task)

        # Wait for both tasks to complete
        # This will run until one task finishes (e.g., disconnect) or raises an exception
        done, pending = await asyncio.wait(
            {receive_task, send_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel any pending tasks if one finishes early (e.g., error in one loop)
        for task in pending:
            logger.info(f"[{session_id or 'Unknown'}] Cancelling pending task: {task.get_name()}")
            task.cancel()
            try:
                await task # Allow cancellation to propagate
            except asyncio.CancelledError:
                 logger.info(f"[{session_id or 'Unknown'}] Task {task.get_name()} cancelled successfully.")
            except Exception as e:
                 logger.error(f"[{session_id or 'Unknown'}] Error during task cancellation for {task.get_name()}: {e}", exc_info=True)


    except websockets.exceptions.InvalidHandshake as e:
         logger.error(f"Failed to connect to OpenAI WebSocket (Handshake): {e}", exc_info=True)
    except Exception as e:
        logger.error(f"[{session_id or 'Unknown'}] Error in handle_twilio_stream main loop: {e}", exc_info=True)
    finally:
        sid_for_log = session_id or "Unknown Session"
        logger.info(f"[{sid_for_log}] Cleaning up session...")

        # Ensure OpenAI connection is closed
        if openai_ws and openai_ws.open:
            logger.info(f"[{sid_for_log}] Closing OpenAI WebSocket.")
            await openai_ws.close()

        # Ensure Twilio connection is closed (FastAPI handles this automatically on function exit/error, but explicit close is safe)
        # try:
        #     if websocket.client_state != websockets.protocol.State.CLOSED:
        #         logger.info(f"[{sid_for_log}] Closing Twilio WebSocket.")
        #         await websocket.close()
        # except Exception as e:
        #     logger.error(f"[{sid_for_log}] Error closing Twilio WebSocket: {e}", exc_info=True)


        # Remove session from active sessions if it exists
        if session_id and session_id in active_sessions:
            logger.info(f"[{sid_for_log}] Removing session from active_sessions.")
            active_sessions.pop(session_id, None)
        else:
            logger.warning(f"[{sid_for_log}] Session ID not found in active_sessions during cleanup.")

        logger.info(f"[{sid_for_log}] Session cleanup complete.")


if __name__ == "__main__":
    import uvicorn
    # Recommended: Run with multiple workers for production, but single worker simplifies in-memory session state
    # Uvicorn handles async task management within the worker process
    logger.info(f"Starting Uvicorn server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT) # Use reload=True for development if needed
