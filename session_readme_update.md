 ▐▛███▛█   Claude Code v2.1.322
▝▜██████▀  Opus 5.5 · Claude Pro
 ▝▝   ▝▝   ~/Downloads/tool-calling-from-scratch


❯  I want you to do me a favor. This whole codebase is completely changed from  
  what it used to be, and the README.md is completely outdated. The session     
  files are also there to give you context.                                     
                                                                                
  Let's do this: the current README.md file is outdated. I want you to create a 
  copy of that, a copy of this README.md file, and then edit that file and      
  update it to that second version of the README file (with all the current     
  updates and documentation, the design decisions that we made, and everything  
  that these session MD files outline). Just so, then it's more of a recap, and 
  also just don't keep the README file extremely long. Keep it detailed but     
  concise, and like something that's easy to process and understand.            

⏺ I'll read the current README, session files, and code first.

  Listed 1 directory, ran 2 shell commands (ctrl+o to expand)

⏺ Now the session files — skimming for decisions and summaries.

  Searched for 1 pattern, read 1 file, ran 2 shell commands (ctrl+o to expand)

⏺ Good picture now. Checking tests and setup details.

  Ran 1 shell command (ctrl+o to expand)

⏺ Writing the updated copy as README_v2.md (original README.md left untouched).

⏺ Bash(cp README.md README_v2.md)
  ⎿  Created README_v2.md (+42 -0)
       1 +# AI Tool Calling from Scratch                                 
       2 +## Overview                                                    
       3 +Imagine you just built the world's first multi-modal chat bot, 
         +and now you want to add tool calling to it. The goal of this co
         +ding challenge is to create a simple library for defining and r
         +unning AI agents that can use tool calling. This should take ar
         +ound 2-3 hours to complete.                                    
       4 +Your implementation can roughly follow the interface defined in
         + `agent.py`.                                                   
       5 +                                                               
       6 +## Main Task: Bounding Box Detection                           
       7 +Create an agent that can detect and return bounding boxes aroun
         +d / return full new versions of specific items in images using 
         +API endpoints from common providers (Gemini, OpenAI, Claude). T
         +he agent should:                                               
       8 +- Accept a photo and text as input                             
       9 +- Use an API library (Gemini, OpenAI, or Claude) to recognize i
         +tems in the image.                                             
      10 +- Return bounding box coordinates for the detected item        
      11 +- Be general enough to handle complex images: multiple objects,
         + cluttered scenes, partial occlusions, varying lighting conditi
         +ons, and diverse backgrounds                                   
      12 +Example use case: Provide a photo of a scene containing a dog, 
         +cat, and tree, and get a bounding box around the dog. (This is 
         +a very simple use case...)                                     
      13 +                                                               
      14 +```python                                                      
      15 +agent = Agent(                                                 
      16 +    tools=[Tool(name="detect_bounding_box", ...)]              
      17 +)                                                              
      18 +agent.run(messages=[Message(role=Role.USER, content="Make a bou
         +nding box around the dog in this image", image="path/to/dog.jpg
         +")])                                                           
      19 +```                                                            
      20 +                                                               
      21 +## Rules                                                       
      22 +- The only non-standard libraries you can use are strictly for 
         +LLM/Vision API calls and Pillow (for image drawing/editing). Yo
         +u can use any LLM or Vision API you want (e.g., Gemini, OpenAI,
         + Claude), but you cannot use any libraries' prebuilt tool calli
         +ng capabilities (because that is exactly what you're making fro
         +m scratch).                                                    
      23 +- Everything else must be implemented through your own code. Th
         +is includes any prompt engineering, agentic logic, state manage
         +ment, response parsing, etc.                                   
      24 +- The `Agent` skeleton in `agent.py` is just a starting point. 
         +Feel free to change/add any methods/classes, create helper clas
         +ses, etc. The implementation of `Tool`, `Message`, and `Role` i
         +s up to you.                                                   
      25 +## Design Considerations                                       
      26 +- What should the agent do if it doesn't have all the informati
         +on it needs to answer the query?                               
      27 +- What should the agent do if it doesn't have the necessary par
         +ameters it needs to call a tool?                               
      28 +- How should the agent decide which tool to call?              
      29 +- How should the agent decide when to stop calling tools?      
      30 +- How should the agent handle errors from tool calls?          
      31 +- How should the agent handle the response from tool calls?    
      32 +                                                               
      33 +## Requirements                                                
      34 +- Implement the `Agent` class in `agent.py`.                   
      35 +- Implement the `Tool` class in `agent.py`.                    
      36 +- Implement the bounding box detection use case in `chat.py` fi
         +le to demonstrate how to use your `Agent` and `Tool` classes wi
         +th a photo of a dog.                                           
      37 +- Output a photo of the original photo that has the bounding bo
         +x overlayed.                                                   
      38 +- Anything beyond these requirements is totally up to you.     
      39 +## Submission                                                  
      40 +- Submit a zip file containing the `agent.py` and `chat.py` fil
         +es.                                                            
     … 2 more lines (ctrl+o to expand)

  Read 1 file (ctrl+o to expand)

⏺ Write(README_v2.md)
  ⎿  Added 147 lines, removed 35 lines
        1  # AI Tool Calling from Scratch
        2 -## Overview                                                   
        3 -Imagine you just built the world's first multi-modal chat bot,
          - and now you want to add tool calling to it. The goal of this 
          -coding challenge is to create a simple library for defining an
          -d running AI agents that can use tool calling. This should tak
          -e around 2-3 hours to complete.                               
        4 -Your implementation can roughly follow the interface defined i
          -n `agent.py`.                                                 
        2  
        6 -## Main Task: Bounding Box Detection                          
        7 -Create an agent that can detect and return bounding boxes arou
          -nd / return full new versions of specific items in images usin
          -g API endpoints from common providers (Gemini, OpenAI, Claude)
          -. The agent should:                                           
        8 -- Accept a photo and text as input                            
        9 -- Use an API library (Gemini, OpenAI, or Claude) to recognize 
          -items in the image.                                           
       10 -- Return bounding box coordinates for the detected item       
       11 -- Be general enough to handle complex images: multiple objects
          -, cluttered scenes, partial occlusions, varying lighting condi
          -tions, and diverse backgrounds                                
       12 -Example use case: Provide a photo of a scene containing a dog,
          - cat, and tree, and get a bounding box around the dog. (This i
          -s a very simple use case...)                                  
        3 +A small agent library with hand-built tool calling, plus a dem
          +o that draws bounding boxes around objects in an image using G
          +emini.                                                        
        4  
        5 +No SDK tool calling is used. The prompt protocol, JSON parsing
          +, argument validation, retry logic and conversation state are 
          +all plain Python. The only third-party libraries are `google-g
          +enai` (to send text and an image to Gemini) and Pillow (to loa
          +d and draw on images).                                        
        6 +                                                              
        7 +```                                                           
        8 +$ python chat.py --image dog.png --prompt "Draw a box around t
          +he dog" --output dog_boxed.png                                
        9 +  [assistant] {"type":"tool_call","name":"draw_bounding_boxes"
          +,"arguments":{"boxes":[{"label":"dog","box":[205,161,835,867]}
          +]}}                                                           
       10 +  [draw_bounding_boxes] {"success": true, "output_path": "dog_
          +boxed.png", "num_boxes": 1, ...}                              
       11 +AI: Drew 1 box(es) (dog). Saved to dog_boxed.png              
       12 +```                                                           
       13 +                                                              
       14 +## The challenge                                              
       15 +                                                              
       16 +Build a minimal library for defining and running agents that c
          +all tools, without using any provider's built-in tool calling.
          + Main use case: given a photo and a text request, return bound
          +ing boxes for the requested object(s) and save a copy of the p
          +hoto with the boxes drawn on it. Only LLM/vision API libraries
          + and Pillow are allowed.                                      
       17 +                                                              
       18 +## Quick start                                                
       19 +                                                              
       20 +```bash                                                       
       21 +pip install google-genai pillow                               
       22 +export GEMINI_API_KEY=your-key          # or GOOGLE_API_KEY   
       23 +export GEMINI_MODEL=gemini-3.8-flash    # optional; this is th
          +e default                                                     
       24 +```                                                           
       25 +                                                              
       26 +If you keep the key in a local `.env` file (it is gitignored),
          + load it into the shell with `set -a; source .env; set +a`. Th
          +e code itself only reads environment variables.               
       27 +                                                              
       28 +**One-shot (preferred):**                                     
       29 +```bash                                                       
       30 +python chat.py --image dog.png --prompt "Draw a box around the
          + dog" --output dog_boxed.png                                  
       31 +python chat.py --image environment.png --prompt "Box the cat a
          +nd the dog"                                                   
       32 +```                                                           
       33 +`--output` is optional. By default results go to `outputs/<nam
          +e>_boxes.png`. If the file already exists, `_2`, `_3`, ... is 
          +added, so earlier results are never overwritten. The input ima
          +ge is never modified.                                         
       34 +                                                              
       35 +**Interactive chat:** run `python chat.py` and write one plain
          + line with the request and the image path:                    
       36 +```                                                           
       37 +You: Draw a box around the dog in dog.png                     
       38 +You: Is there a car in environment.png?     # answers in text,
          + draws nothing                                                
       39 +You: now box the tree                        # follow-up reuse
          +s the last image                                              
       40 +```                                                           
       41 +CLI flags (`--image` etc.) don't work at the `You:` prompt. Th
          +e chat says so and makes no model call.                       
       42 +                                                              
       43 +## Files                                                      
       44 +                                                              
       45 +| File | What it holds |                                      
       46 +|---|---|                                                     
       47 +| `agent.py` | The reusable library: `Role`, `Message`, `Tool`
          +, `Agent`, strict reply parser, system prompt, image loader. K
          +nows nothing about Gemini. |                                  
       48 +| `chat.py` | The demo: the `draw_bounding_boxes` tool and its
          + validator, the Gemini provider, image preparation, error clas
          +sification, CLI and chat loop. |                              
       49 +| `tests/` | 47 offline tests (`unittest` only, no API key, no
          + network). |                                                  
       50 +| `session*.md` | Transcripts of the build sessions, where the
          + decisions below were made. |                                 
       51 +                                                              
       52 +## How it works                                               
       53 +                                                              
       54 +```                                                           
       55 +user text + image path                                        
       56 +        │                                                     
       57 +        ▼                                                     
       58 +Agent.run ── load_image (JPEG/PNG only, verified, EXIF-rotated
          +)                                                             
       59 +        │                                                     
       60 +        ▼  up to max_attempts (3)                             
       61 +  llm(system_prompt, messages, image) ──► raw text            
       62 +        │                                                     
       63 +  parse_reply (strict JSON)                                   
       64 +   ├─ {"type":"final","message":...}            ──► return the
          + answer                                                       
       65 +   └─ {"type":"tool_call","name":...,"arguments":{...}}       
       66 +            │                                                 
       67 +        call_tool: known tool? exact args? validate() passes? 
          +signature binds?                                              
       68 +            ├─ yes ──► run the tool ──► success ends the turn 
          +(no extra model call)                                         
       69 +            └─ no  ──► error goes back into the conversation, 
          +model tries again                                             
       70 +```                                                           
       71 +                                                              
       72 +### The protocol                                              
       73 +                                                              
       74 +The model must reply with exactly one JSON object, nothing els
          +e, in one of two shapes:                                      
       75 +                                                              
       76 +```json                                                       
       77 +{"type":"tool_call","name":"draw_bounding_boxes","arguments":{
          +"boxes":[{"label":"dog","box":[630,680,815,895]}]}}           
       78 +{"type":"final","message":"The dog was not found."}           
       79 +```                                                           
       80 +                                                              
       81 +`final` is how the model answers without a tool: the object is
          +n't in the image, the request is unclear, or no tool fits.    
       82 +                                                              
       83 +### Messages and roles                                        
       84 +                                                              
       85 +`Message(role, content, image_path=None, tool_name=None)` with
          + four roles:                                                  
       86 +                                                              
       87 +- `USER`: the request, with an optional `image_path`.         
       88 +- `ASSISTANT`: the model's raw JSON reply, stored once, as sen
          +t.                                                            
       89 +- `TOOL`: the result of a Python tool function. It must carry 
          +a `tool_name`, and no other role may have one.                
       90 +- `SYSTEM`: rejection notes sent back to the model ("your repl
          +y was rejected: ...").                                        
       91 +                                                              
       92 +Every step is recorded in the shared message list, so the trai
          +l shows which tool produced which result. The CLI prints it, a
          +nd it makes debugging and testing straightforward.            
       93 +                                                              
       94 +### Tools                                                     
       95 +                                                              
       96  ```python
       15 -agent = Agent(                                                
       16 -    tools=[Tool(name="detect_bounding_box", ...)]             
       17 -)                                                             
       18 -agent.run(messages=[Message(role=Role.USER, content="Make a bo
          -unding box around the dog in this image", image="path/to/dog.j
          -pg")])                                                        
       97 +Tool(name, description, parameter_schema, function, validate=N
          +one)                                                          
       98  ```
       99  
       21 -## Rules                                                      
       22 -- The only non-standard libraries you can use are strictly for
          - LLM/Vision API calls and Pillow (for image drawing/editing). 
          -You can use any LLM or Vision API you want (e.g., Gemini, Open
          -AI, Claude), but you cannot use any libraries' prebuilt tool c
          -alling capabilities (because that is exactly what you're makin
          -g from scratch).                                              
       23 -- Everything else must be implemented through your own code. T
          -his includes any prompt engineering, agentic logic, state mana
          -gement, response parsing, etc.                                
       24 -- The `Agent` skeleton in `agent.py` is just a starting point.
          - Feel free to change/add any methods/classes, create helper cl
          -asses, etc. The implementation of `Tool`, `Message`, and `Role
          -` is up to you.                                               
       25 -## Design Considerations                                      
       26 -- What should the agent do if it doesn't have all the informat
          -ion it needs to answer the query?                             
       27 -- What should the agent do if it doesn't have the necessary pa
          -rameters it needs to call a tool?                             
       28 -- How should the agent decide which tool to call?             
       29 -- How should the agent decide when to stop calling tools?     
       30 -- How should the agent handle errors from tool calls?         
       31 -- How should the agent handle the response from tool calls?   
      100 +- `parameter_schema` lists every argument. All are required, a
          +nd no others are allowed. It is shown to the model in the syst
          +em prompt.                                                    
      101 +- `validate(**arguments)` raises `TypeError`/`ValueError` on b
          +ad values. It runs before the function, so bad model data neve
          +r reaches the tool.                                           
      102 +- `function` gets the model's arguments, plus any of `original
          +_image`, `image_path` and `output_path` that it declares. Thes
          +e come from Python and never from the model, and tools that do
          +n't need images simply leave them out.                        
      103 +- Bad definitions (empty or non-string names, non-dict schema,
          + non-callable function) and duplicate tool names fail at start
          +up, before any API call.                                      
      104  
       33 -## Requirements                                               
       34 -- Implement the `Agent` class in `agent.py`.                  
       35 -- Implement the `Tool` class in `agent.py`.                   
       36 -- Implement the bounding box detection use case in `chat.py` f
          -ile to demonstrate how to use your `Agent` and `Tool` classes 
          -with a photo of a dog.                                        
       37 -- Output a photo of the original photo that has the bounding b
          -ox overlayed.                                                 
       38 -- Anything beyond these requirements is totally up to you.    
       39 -## Submission                                                 
       40 -- Submit a zip file containing the `agent.py` and `chat.py` fi
          -les.                                                          
       41 -- Show an example of an agent in action.                      
       42 -- Give a brief explanation of your design decisions and how yo
          -u implemented the agent.                                      
      105   No newline at end of file
      106 +## Design decisions                                           
      107 +                                                              
      108 +**Strict parsing, no rescue.** `parse_reply` runs `json.loads`
          + on the whole reply. Prose around the JSON, code fences, sever
          +al objects, duplicate keys, `NaN`/`Infinity`, and missing or e
          +xtra fields are all rejected. The model gets one clear correct
          +ion message instead of the parser guessing at what it meant. T
          +he system prompt also tells it explicitly not to use code fenc
          +es.                                                           
      109 +                                                              
      110 +**Retries are sorted by cause.**                              
      111 +                                                              
      112 +| Problem | What happens |                                    
      113 +|---|---|                                                     
      114 +| Malformed JSON, unknown tool, missing/extra/invalid argument
          +s | Error goes back to the model and uses one of 3 attempts | 
      115 +| Empty or blocked response | Stops at once (there is nothing 
          +to correct) |                                                 
      116 +| Tool crash, disk or output path error | Stops at once (the m
          +odel can't fix it) |                                          
      117 +| Timeout, 408, 429, 5xx | One quick retry inside the same ste
          +p (doesn't use a model attempt) |                             
      118 +| Bad API key, unknown model, 400, 403 | Stops at once with a 
          +specific message |                                            
      119 +                                                              
      120 +**Success ends the turn.** A successful tool result is not sen
          +t back to Gemini for a summary. The tool's own message is the 
          +answer, which saves an API call per request.                  
      121 +                                                              
      122 +**Only labels and coordinates come from the model.** File path
          +s, output locations, the image and API keys stay in Python. In
          + chat, the image path is replaced with "the image" before the 
          +text goes to Gemini, so local paths are never sent. Labels mus
          +t be plain object names (letters, digits, spaces, `-` and `'`,
          + at most 40 characters), so `black_cat`, `dog.png` and `a/b` a
          +re rejected.                                                  
      123 +                                                              
      124 +**Box format.** Each box is `[y_min, x_min, y_max, x_max]` as 
          +whole numbers from 0 to 1000, normalized to the image's height
          + and width. This is Gemini's native detection format. The vali
          +dator rejects floats, booleans, null, strings, values out of r
          +ange and inverted boxes. When drawing, coordinates are convert
          +ed to pixels and clamped so that 1000 maps to the last pixel (
          +`w-1`/`h-1`).                                                 
      125 +                                                              
      126 +**Three kinds of image, kept apart.**                         
      127 +- *Original*: the user's file. It is never changed.           
      128 +- *Gemini copy*: exists in memory only. It is compressed or sh
          +runk only if the request would be too big.                    
      129 +- *Boxed result*: a new file, drawn on the full-quality origin
          +al.                                                           
      130 +                                                              
      131 +Because boxes are normalized and resizing keeps the aspect rat
          +io, a smaller copy gives the same boxes.                      
      132 +                                                              
      133 +**Request size budget.** Gemini's inline request limit is 100 
          +MB, and the code keeps each request under 90 MB. The size esti
          +mate includes base64 growth (every 3 bytes become 4), the prom
          +pt text, and some overhead. The copy is tried in this order: f
          +ull-size PNG (only for PNG inputs up to 50 MB raw, so screensh
          +ots stay lossless), then full-size JPEG (q85), then JPEG shrun
          +k to 75% of its width and height per step. If the long side wo
          +uld go below 768 px, it stops with an `image_too_large` error 
          +before sending anything. The copy is never cropped, stretched 
          +or rotated. CMYK, 16-bit, palette and transparent images are c
          +onverted to plain RGB, with transparency flattened onto white.
      134 +                                                              
      135 +**Clear, specific errors.** API failures are sorted into `inva
          +lid_api_key`, `model_not_found`, `rate_limited`, `timeout`, `n
          +etwork_error`, `server_error`, `image_too_large`, `empty_respo
          +nse` and so on, each with what to do next. Requests time out a
          +fter 30 s.                                                    
      136 +                                                              
      137 +**Input checks before any model call.** The image must exist a
          +nd be a still JPEG or PNG. `--output` must end in `.png`/`.jpg
          +`/`.jpeg`, its folder must exist, and it can't be the input fi
          +le. In chat, a message naming two images, or an image that doe
          +sn't exist, gets an answer without calling Gemini.            
      138 +                                                              
      139 +**Full conversation is sent.** Gemini sees the whole history s
          +o follow-ups work. The image is attached once, to the newest m
          +essage that named it.                                         
      140 +                                                              
      141 +**Provider is a plain function.** `LLM = (system_prompt, messa
          +ges, image) -> str`. The agent can run against a scripted fake
          + model, and that is how the tests work. Swapping in another pr
          +ovider means writing one function.                            
      142 +                                                              
      143 +## Tests                                                      
      144 +                                                              
      145 +```bash                                                       
      146 +python -m unittest discover -s tests -v                       
      147 +```                                                           
      148 +                                                              
      149 +| File | Covers |                                             
      150 +|---|---|                                                     
      151 +| `test_agent_unit.py` (17) | Message/tool validation, system 
          +prompt, strict parsing, `call_tool` errors, `Agent.run` stop/r
          +etry rules, image loading |                                   
      152 +| `test_chat_unit.py` (26) | Box and label validation, image b
          +udget and resizing, color-mode conversion, coordinate clamping
          +, output paths, chat path parsing, CLI flag detection, API err
          +or classification |                                           
      153 +| `test_integration.py` (4) | Real agent and drawing code with
          + a scripted fake Gemini: valid call, malformed JSON then valid
          +, invalid coordinates then valid |                            
      154 +                                                              
      155 +Related cases are grouped with `subTest` tables. Outputs go to
          + temp folders, and nothing touches the network.               

  Searched for 1 pattern (ctrl+o to expand)

⏺ Update(README_v2.md)
  ⎿  Added 1 line, removed 1 line
      149  |---|---|
      150  | `test_agent_unit.py` (17) | Message/tool validation, system 
           prompt, strict parsing, `call_tool` errors, `Agent.run` stop/r
           etry rules, image loading |
      151  | `test_chat_unit.py` (26) | Box and label validation, image b
           udget and resizing, color-mode conversion, coordinate clamping
           , output paths, chat path parsing, CLI flag detection, API err
           or classification |
      152 -| `test_integration.py` (4) | Real agent and drawing code with
          - a scripted fake Gemini: valid call, malformed JSON then valid
          -, invalid coordinates then valid |                            
      152 +| `test_integration.py` (4) | Real agent and drawing code with
          + a scripted fake Gemini: valid call, box lands on the right pi
          +xels, malformed JSON then valid, invalid coordinates then vali
          +d |                                                           
      153  
      154  Related cases are grouped with `subTest` tables. Outputs go to
            temp folders, and nothing touches the network.

⏺ I've written the updated README as README_v2.md (about 155 lines). The
  original README.md is untouched. Every claim in it was checked against the
  current code, and all 47 tests pass.

  What's in it:
  - Intro, demo run and challenge recap: a short output sample, then the
    original assignment shrunk to one paragraph.
  - Quick start: install, export GEMINI_API_KEY, the --image/--prompt/--output
    flags, interactive chat, and how output files get _2, _3 names instead of
    overwriting.
  - Files table: what agent.py, chat.py, tests/ and the session files each hold.
  - How it works: a flow diagram of the agent loop, the two JSON reply shapes,
    the four message roles, and how tools are defined.
  - Design decisions from the sessions:
    - strict JSON parsing with no attempt to rescue bad replies
    - a table of which errors get a retry and which stop the run
    - a successful tool call ends the turn with no extra Gemini call
    - only labels and coordinates come from the model, and local paths are never
      sent to it
    - the 0–1000 box format and how it's clamped to real pixels
    - the original image, the Gemini copy and the boxed result are kept separate
    - the 90 MB request budget under Gemini's 100 MB limit, with
      compress-then-shrink
    - the specific API error types
  - Tests: how to run them and what each file covers.

  Two choices to check:
  - The .env file: the README says it can be loaded with set -a; source .env; 
    set +a. That matches your session 3 decision not to add a dotenv dependency.
  - The old test_agent.py: I left it out of the files table, since you planned
    to delete it now that the tests/ suite is done. Want me to delete it?

  I haven't committed anything. When you're happy with it, I can replace
  README.md with this version and commit, or rename the old one to
  README_original.md.

✻ Brewed for 1m 21s · done 8:42 PM