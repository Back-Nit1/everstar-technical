# AI Tool Calling from Scratch
## Overview
Imagine you just built the world's first multi-modal chat bot, and now you want to add tool calling to it. The goal of this coding challenge is to create a simple library for defining and running AI agents that can use tool calling. This should take around 2-3 hours to complete.
Your implementation can roughly follow the interface defined in `agent.py`.

## Main Task: Bounding Box Detection
Create an agent that can detect and return bounding boxes around / return full new versions of specific items in images using API endpoints from common providers (Gemini, OpenAI, Claude). The agent should:
- Accept a photo and text as input
- Use an API library (Gemini, OpenAI, or Claude) to recognize items in the image.
- Return bounding box coordinates for the detected item
- Be general enough to handle complex images: multiple objects, cluttered scenes, partial occlusions, varying lighting conditions, and diverse backgrounds
Example use case: Provide a photo of a scene containing a dog, cat, and tree, and get a bounding box around the dog. (This is a very simple use case...)

```python
agent = Agent(
    tools=[Tool(name="detect_bounding_box", ...)]
)
agent.run(messages=[Message(role=Role.USER, content="Make a bounding box around the dog in this image", image="path/to/dog.jpg")])
```

## Rules
- The only non-standard libraries you can use are strictly for LLM/Vision API calls and Pillow (for image drawing/editing). You can use any LLM or Vision API you want (e.g., Gemini, OpenAI, Claude), but you cannot use any libraries' prebuilt tool calling capabilities (because that is exactly what you're making from scratch).
- Everything else must be implemented through your own code. This includes any prompt engineering, agentic logic, state management, response parsing, etc.
- The `Agent` skeleton in `agent.py` is just a starting point. Feel free to change/add any methods/classes, create helper classes, etc. The implementation of `Tool`, `Message`, and `Role` is up to you.
## Design Considerations
- What should the agent do if it doesn't have all the information it needs to answer the query?
- What should the agent do if it doesn't have the necessary parameters it needs to call a tool?
- How should the agent decide which tool to call?
- How should the agent decide when to stop calling tools?
- How should the agent handle errors from tool calls?
- How should the agent handle the response from tool calls?

## Requirements
- Implement the `Agent` class in `agent.py`.
- Implement the `Tool` class in `agent.py`.
- Implement the bounding box detection use case in `chat.py` file to demonstrate how to use your `Agent` and `Tool` classes with a photo of a dog.
- Output a photo of the original photo that has the bounding box overlayed.
- Anything beyond these requirements is totally up to you.
## Submission
- Submit a zip file containing the `agent.py` and `chat.py` files.
- Show an example of an agent in action.
- Give a brief explanation of your design decisions and how you implemented the agent.