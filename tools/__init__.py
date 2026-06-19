"""Earth Rover Mini — Strands @tool wrappers.

Organizing principle:
    Each module wraps ONE capability domain of the rover via the
    Earth Rovers SDK HTTP API. Camera tools return PROPER Strands
    inline image content blocks so the model can see.

    from tools import ROVER_ALL_TOOLS
    agent = Agent(tools=[*ROVER_ALL_TOOLS, shell, ...])
"""
from .rover_camera import rover_see, rover_screenshot
from .rover_motion import rover_move, rover_stop, rover_lamp
from .rover_navigate import rover_navigate
from .rover_record import (
    controller_start,
    controller_status,
    controller_stop,
    recording_status,
    start_recording,
    stop_recording,
)
from .rover_state import rover_speak, rover_state
from .rover_memory import rover_memory
from .telegram import telegram
from .voice_bridge import voice_say

ROVER_VISION_TOOLS = [rover_see, rover_screenshot]
ROVER_MOTION_TOOLS = [rover_move, rover_navigate, rover_stop, rover_lamp]
ROVER_STATE_TOOLS = [rover_state, rover_speak]
ROVER_MEMORY_TOOLS = [rover_memory]
ROVER_COMMS_TOOLS = [telegram, voice_say]
ROVER_RECORDING_TOOLS = [start_recording, stop_recording, recording_status]
ROVER_CONTROLLER_TOOLS = [controller_start, controller_stop, controller_status]

ROVER_ALL_TOOLS = [
    *ROVER_VISION_TOOLS,
    *ROVER_MOTION_TOOLS,
    *ROVER_STATE_TOOLS,
    *ROVER_MEMORY_TOOLS,
    *ROVER_RECORDING_TOOLS,
    *ROVER_CONTROLLER_TOOLS,
    *ROVER_COMMS_TOOLS,
]

__all__ = [
    "rover_see", "rover_screenshot",
    "rover_move", "rover_navigate", "rover_stop", "rover_lamp",
    "rover_state",
    "rover_memory", "rover_speak",
    "start_recording", "stop_recording", "recording_status",
    "controller_start", "controller_stop", "controller_status",
    "ROVER_ALL_TOOLS",
    "ROVER_VISION_TOOLS", "ROVER_MOTION_TOOLS", "ROVER_STATE_TOOLS",
    "ROVER_RECORDING_TOOLS", "ROVER_CONTROLLER_TOOLS", "ROVER_COMMS_TOOLS",
    "telegram", "voice_say",
]
