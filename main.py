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
SYSTEM_MESSAGE = (
    "Hey Ethan, I'm your AI interviewer—great to connect with you! "
    "Before we dive into internship details, how's your day been so far? Feel free to take a moment to gather your thoughts. "
    "Here's our plan: "
    "First, tell me your story—what inspired you to pursue this field and get into this role? "
    "Next, share the skills and experiences you bring to our team. Take your time. "
    "Finally, let's talk about what excites you most about this internship opportunity. "
    "I'll follow up on your answers, ensuring we stay focused on the internship. If you stray off-topic, I'll prompt you with, 'Nice, how does that relate to the internship?' "
    "Remember to speak slowly and clearly—I'm here to make this a comfortable and engaging conversation. Let's get started!"
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

# Define DEFAULT_SYSTEM_PROMPT using the original SYSTEM_MESSAGE
DEFAULT_SYSTEM_PROMPT = SYSTEM_MESSAGE

# --- Pydantic Models ---
class MakeCallRequest(BaseModel):
    candidate_id: uuid.UUID = Field(..., description="The UUID of the candidate to call")

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
async def make_outbound_call(call_request: MakeCallRequest): # Accept request body
    candidate_id = call_request.candidate_id
    print(f"Attempting to make call to candidate_id: {candidate_id}")
    try:
        # Step 1: Retrieve candidate, including name and job_id
        candidate_response = (
            supabase.table("candidates")
            .select("id, phone_number, name, job_id") # Added name, job_id
            .eq("id", str(candidate_id))
            .limit(1)
            .maybe_single() # Use maybe_single for cleaner handling
            .execute()
        )

        candidate = candidate_response.data
        if not candidate:
            print(f"Candidate with ID {candidate_id} not found.")
            return JSONResponse({"error": f"Candidate with ID {candidate_id} not found"}, status_code=404)

        phone_number = candidate.get("phone_number")
        candidate_name = candidate.get("name", "Candidate") # Default if name is missing
        job_id = candidate.get("job_id")

        if not phone_number:
            print(f"Candidate {candidate_id} has no phone number.")
            return JSONResponse({"error": f"Candidate {candidate_id} has no phone number"}, status_code=400)

        # Step 1b: Retrieve job title
        job_title = "the role" # Default if job_id is missing or job not found
        if job_id:
            job_response = (
                supabase.table("jobs")
                .select("title")
                .eq("id", str(job_id))
                .limit(1)
                .maybe_single()
                .execute()
            )
            if job_response.data and job_response.data.get("title"):
                job_title = job_response.data["title"]
            else:
                print(f"WARN: Job with ID {job_id} not found for candidate {candidate_id}.")

        # Step 2: Generate the custom system prompt
        custom_system_prompt = (
            f"Hey {candidate_name}, I'm your AI interviewer—great to connect with you about the {job_title} position! "
            "Before we dive into the details, how's your day been so far? Feel free to take a moment to gather your thoughts. "
            "Here's our plan: "
            "First, tell me your story—what inspired you to pursue this field and apply for this role? "
            "Next, share the skills and experiences you bring that are relevant to the {job_title} position. Take your time. "
            "Finally, let's talk about what excites you most about this specific opportunity. "
            "I'll follow up on your answers, ensuring we stay focused on the internship. If you stray off-topic, I'll prompt you with, 'Nice, how does that relate to the {job_title} role?' "
            f"Remember to speak slowly and clearly—I'm here to make this a comfortable and engaging conversation. Let's get started!"
        )
        print(f"Generated custom prompt for call with {candidate_name} for job {job_title}")

        # Step 3: Use Twilio to initiate the call
        twilio_callback_url = f"{PUBLIC_SERVER_URL}/incoming-call"
        print(f"Setting Twilio callback URL to: {twilio_callback_url}")
        call = client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=twilio_callback_url
        )
        call_sid = call.sid # Get the call SID immediately

        # Step 4: Log the call, including the custom system prompt and call_sid
        insert_response = supabase.table("call_logs").insert({
            "id": str(uuid.uuid4()), # Generate new UUID for call_log
            "call_sid": call_sid,    # Store the Twilio Call SID
            "candidate_id": str(candidate_id),
            "status": "initiated",
            "started_at": datetime.now().isoformat(),
            "system_prompt": custom_system_prompt # Store the generated prompt
        }).execute()

        # Error checking for insert
        if hasattr(insert_response, 'error') and insert_response.error:
             print(f"ERROR inserting call log: {insert_response.error}")
             # Optional: proceed with call but log error, or return error

        # Step 5: Return JSON response
        return JSONResponse({
            "status": "calling",
            "candidate_id": str(candidate_id),
            "call_sid": call_sid
        })

    except Exception as e:
        import traceback
        print(f"Error during make_outbound_call for candidate {candidate_id}:")
        traceback.print_exc()
        return JSONResponse({"error": f"An internal error occurred: {e}"}, status_code=500)


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("Client connected")
    await websocket.accept()

    # Variables accessible by nested functions
    openai_ws_global = None
    call_sid = None
    stream_sid = None

    url = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01'
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1")
    ]

    try:
        async with websockets.connect(url, extra_headers=headers) as openai_ws:
            openai_ws_global = openai_ws
            print("Connected to OpenAI WebSocket")
            # Session update is now sent inside receive_from_twilio after 'start'

            async def receive_from_twilio():
                nonlocal stream_sid, call_sid # Allow modification
                session_update_sent = False # Flag to send session update only once
                try:
                    async for message in websocket.iter_text():
                        if not openai_ws.open:
                            print("OpenAI WS closed, stopping receive loop.")
                            break
                        try:
                            data = json.loads(message)
                            event = data.get('event')

                            if event == 'start':
                                stream_sid_local = data.get('start', {}).get('streamSid')
                                call_sid_local = data.get('start', {}).get('callSid')
                                print(f"Incoming stream started: SID {stream_sid_local}, Call SID: {call_sid_local}")

                                if not stream_sid_local or not call_sid_local:
                                    err_msg = f"Missing {'streamSid' if not stream_sid_local else 'callSid'} in 'start' event."
                                    print(f"ERROR: {err_msg}")
                                    await websocket.close(code=1003, reason=err_msg)
                                    break

                                # Assign to outer scope variables
                                stream_sid = stream_sid_local
                                call_sid = call_sid_local

                                # Fetch prompt and send session update ONCE
                                if not session_update_sent and openai_ws.open:
                                    fetched_prompt = await get_call_system_prompt(call_sid)
                                    prompt_to_use = fetched_prompt or DEFAULT_SYSTEM_PROMPT # Use fetched or default
                                    print(f"Determined prompt for call {call_sid}. Sending session update.")
                                    await send_session_update(openai_ws, prompt_to_use)
                                    session_update_sent = True
                                elif not session_update_sent: # Log if WS closed before sending
                                     print("WARN: Cannot send session update, OpenAI WS closed after start event processing.")


                            elif event == 'media':
                                if not session_update_sent:
                                    print("WARN: Received media before session update sent (start not processed?). Discarding.")
                                    continue

                                payload = data.get('media', {}).get('payload')
                                if payload and openai_ws.open:
                                    audio_append = {
                                        "type": "input_audio_buffer.append",
                                        "audio": payload
                                    }
                                    await openai_ws.send(json.dumps(audio_append))
                                elif not payload:
                                    print("WARN: Received media event with no payload.")
                                elif not openai_ws.open:
                                     print("WARN: Cannot send media, OpenAI WS closed.")


                            elif event == 'stop':
                                print(f"Stop event received for call {call_sid}. Initiating close.")
                                if openai_ws.open:
                                    stop_message = {"type": "input_text.interrupt"}
                                    try:
                                        await openai_ws.send(json.dumps(stop_message))
                                    except websockets.ConnectionClosed:
                                        print("OpenAI WS already closed when sending stop interrupt.")
                                break # Exit loop on stop

                            elif event == 'mark':
                                mark_name = data.get('mark', {}).get('name')
                                print(f"Mark event received: {mark_name}")

                        except json.JSONDecodeError as e:
                            print(f"Error decoding JSON from Twilio: {e} - Message: {message[:100]}...")
                        except websockets.ConnectionClosed as e:
                            print(f"OpenAI WS Connection closed while processing Twilio message: {e}")
                            break
                        except Exception as e:
                            import traceback
                            print(f"Error processing Twilio message for call {call_sid}:")
                            traceback.print_exc()

                except WebSocketDisconnect as e:
                    print(f"Twilio WebSocket disconnected: {e.code} {e.reason}")
                except websockets.ConnectionClosed:
                    print("OpenAI WebSocket closed during receive loop (while waiting on Twilio).")
                except Exception as e:
                    import traceback
                    print(f"Unexpected error in receive_from_twilio main loop for call {call_sid}:")
                    traceback.print_exc()
                finally:
                    print(f"Exiting receive_from_twilio loop for call {call_sid}.")

            async def send_to_twilio():
                nonlocal stream_sid # Use stream_sid from outer scope
                try:
                    async for openai_message in openai_ws:
                        if websocket.client_state != websockets.protocol.State.OPEN:
                            print("Twilio WebSocket closed, stopping send loop.")
                            break
                        try:
                            response = json.loads(openai_message)
                            if response['type'] == 'session.updated':
                                print("OpenAI Session updated successfully.") # Less verbose
                            elif response['type'] == 'response.audio.delta' and response.get('delta'):
                                if not stream_sid:
                                    print("WARN: Received audio delta before stream_sid set. Discarding.")
                                    continue

                                audio_payload = base64.b64encode(
                                    base64.b64decode(response['delta'])).decode('utf-8')
                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid, # Use the stream_sid
                                    "media": {"payload": audio_payload}
                                }
                                await websocket.send_json(audio_delta)
                        except json.JSONDecodeError:
                             print(f"Error decoding JSON from OpenAI: {openai_message[:100]}...")
                        except KeyError as e:
                             print(f"Missing key in OpenAI message: {e}, data: {response}")
                        except WebSocketDisconnect:
                             print("Twilio WebSocket disconnected while trying to send.")
                             break
                        except Exception as e:
                             print(f"Error processing OpenAI message or sending to Twilio: {e}")
                except websockets.ConnectionClosedOK:
                    print("OpenAI WebSocket closed normally.")
                except websockets.ConnectionClosedError as e:
                    print(f"OpenAI WebSocket closed with error: {e}")
                except Exception as e:
                    import traceback
                    print(f"Unexpected error in send_to_twilio for call {call_sid}:")
                    traceback.print_exc()
                finally:
                    print(f"Exiting send_to_twilio loop for call {call_sid}.")

            # Run the tasks
            print(f"Starting gather for receive/send tasks for call {call_sid}")
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
            print(f"Gather finished for receive/send tasks for call {call_sid}")

    except websockets.exceptions.InvalidHandshake as e:
         print(f"Failed OpenAI WebSocket handshake: {e}")
    except WebSocketDisconnect as e:
         print(f"Twilio WebSocket disconnected during setup or main handler: {e.code} {e.reason}")
    except ConnectionRefusedError:
        print("ERROR: Connection refused connecting to OpenAI.")
    except Exception as e:
        import traceback
        print(f"An unexpected error occurred in handle_media_stream outer block:")
        traceback.print_exc()
    finally:
        # Use the call_sid defined in the outer scope for logging
        final_call_sid = call_sid if call_sid else 'N/A'
        print(f"handle_media_stream finishing for call {final_call_sid}. Cleaning up...")
        # Ensure both websockets are closed
        if openai_ws_global and openai_ws_global.open:
            print("Finally: Closing OpenAI WS")
            try: await openai_ws_global.close()
            except Exception as close_err: print(f"Error closing OpenAI WS in finally: {close_err}")
        if websocket.client_state == websockets.protocol.State.OPEN:
             print("Finally: Closing Twilio WS")
             try: await websocket.close(code=1000)
             except Exception as close_err: print(f"Error closing Twilio WS in finally: {close_err}")
        print(f"handle_media_stream finished execution for call {final_call_sid}.")


# Modified send_session_update to accept prompt and check connection
async def send_session_update(openai_ws, system_prompt: str): # Accept system_prompt
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 300},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": system_prompt, # Use the passed system_prompt
            "modalities": ["text", "audio"],
            "temperature": 0.7,
        }
    }
    print(f"Sending session update with prompt: '{system_prompt[:50]}...'")
    if openai_ws and openai_ws.open:
        try:
            await openai_ws.send(json.dumps(session_update))
            print("Session update sent successfully.")
        except websockets.ConnectionClosed:
            print("ERROR: Failed to send session update, OpenAI WebSocket closed.")
        except Exception as e:
            print(f"ERROR sending session update: {e}")
    else:
        print("ERROR: Cannot send session update, OpenAI WebSocket is not open.")


# --- Helper Functions ---
async def get_call_system_prompt(call_sid: str) -> str | None:
    """Fetches the system prompt associated with a call_sid from Supabase."""
    if not call_sid:
        print("WARN: get_call_system_prompt called with no call_sid.")
        return None
    try:
        print(f"Fetching system prompt for call_sid: {call_sid}")
        # Using parentheses for implicit continuation
        response = (
            supabase.table("call_logs")
            .select("system_prompt")
            .eq("call_sid", call_sid)
            .limit(1)
            .maybe_single()
            .execute()
        )

        if response.data and response.data.get('system_prompt'):
            print(f"Found custom system prompt for call {call_sid}")
            return response.data['system_prompt']
        else:
            print(f"No specific system prompt found for call {call_sid}. Will use default.")
            return None
    except Exception as e:
        print(f"ERROR fetching system prompt for call {call_sid}: {e}")
        return None # Return None on error, default will be used

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
