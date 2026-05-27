---
title: Obsidian Complete Beginner Guide
description: A comprehensive guide to every Obsidian feature with practical examples
tags:
  - obsidian
  - guide
  - beginner
  - tutorial
created: '2026-02-05'
language: English
---
# Obsidian Complete Beginner Guide

> A comprehensive guide to every Obsidian feature with practical examples.

---

## Table of Contents

1. [[#1. Getting Started]]
2. [[#2. Basic Editing & Markdown]]
3. [[#3. Links & Backlinks]]
4. [[#4. Tags]]
5. [[#5. Folders & File Management]]
6. [[#6. Search]]
7. [[#7. Graph View]]
8. [[#8. Canvas]]
9. [[#9. Templates]]
10. [[#10. Daily Notes]]
11. [[#11. Outline]]
12. [[#12. Bookmarks (Starred)]]
13. [[#13. Command Palette]]
14. [[#14. Quick Switcher]]
15. [[#15. Properties (Frontmatter / YAML)]]
16. [[#16. Callouts]]
17. [[#17. Embeds & Transclusion]]
18. [[#18. Split Panes & Workspaces]]
19. [[#19. Appearance & Themes]]
20. [[#20. Hotkeys (Keyboard Shortcuts)]]
21. [[#21. Core Plugins Overview]]
22. [[#22. Community Plugins]]
23. [[#23. Obsidian Sync]]
24. [[#24. Obsidian Publish]]
25. [[#25. File Recovery & Version History]]
26. [[#26. Slash Commands]]
27. [[#27. Audio Recorder]]
28. [[#28. Word Count & Reading Time]]
29. [[#29. Vim Mode (Editor)]]
30. [[#30. Tips & Best Practices]]

---

## 1. Getting Started

### What is Obsidian?
Obsidian is a **local-first** markdown note-taking app that stores your files as plain `.md` files on your computer. Your data is yours — no cloud lock-in.

### Key Concepts
- **Vault**: A folder on your computer that Obsidian uses as its workspace. All your notes live here.
- **Note**: A single markdown (`.md`) file.
- **Link**: A connection between notes using `[[double brackets]]`.

### Creating Your First Vault
1. Open Obsidian
2. Click **"Create new vault"**
3. Name it (e.g., `My Knowledge Base`)
4. Choose a location on your computer
5. Click **Create**

### Creating Your First Note
1. Click the **"New note"** icon (📄) in the left sidebar, or press `Ctrl/Cmd + N`
2. Type a title at the top
3. Start writing below

**Example:**
```markdown
# My First Note

Welcome to my Obsidian vault! This is where I'll store all my knowledge.
```

---

## 2. Basic Editing & Markdown

Obsidian uses **Markdown** for formatting. Here's everything you need:

### Headings
```markdown
# Heading 1
## Heading 2
### Heading 3
#### Heading 4
##### Heading 5
###### Heading 6
```

### Text Formatting
```markdown
**bold text**
*italic text*
~~strikethrough~~
==highlighted text==
**_bold and italic_**
```

**Result:**
- **bold text**
- *italic text*
- ~~strikethrough~~
- ==highlighted text==
- ***bold and italic***

### Lists

**Unordered list:**
```markdown
- Item 1
- Item 2
  - Sub-item 2a
  - Sub-item 2b
```

**Ordered list:**
```markdown
1. First step
2. Second step
3. Third step
```

**Checklist (Task list):**
```markdown
- [x] Completed task
- [ ] Incomplete task
- [ ] Another task to do
```

### Blockquotes
```markdown
> This is a blockquote.
> It can span multiple lines.
>
> > And you can nest them.
```

### Code

**Inline code:**
```markdown
Use the `print()` function in Python.
```

**Code block:**
````markdown
```python
def hello():
    print("Hello, Obsidian!")
```
````

### Horizontal Rule
```markdown
---
```

### Tables
```markdown
| Feature    | Status  | Notes          |
|------------|---------|----------------|
| Markdown   | ✅ Done | Basic support  |
| Links      | ✅ Done | Wiki-style     |
| Graph View | ✅ Done | Interactive    |
```

### Footnotes
```markdown
Here is a sentence with a footnote.[^1]

[^1]: This is the footnote content.
```

### Math (LaTeX)

**Inline math:**
```markdown
The formula is $E = mc^2$.
```

**Block math:**
```markdown
$$
\sum_{i=1}^{n} x_i = x_1 + x_2 + \cdots + x_n
$$
```

### Comments (Hidden Text)
```markdown
%%This text won't appear in preview mode%%
```

---

## 3. Links & Backlinks

Links are the **core power** of Obsidian. They create connections between your notes.

### Internal Links
```markdown
[[Note Name]]
```
**Example:** `[[My First Note]]` creates a link to your "My First Note" file.

### Link with Display Text
```markdown
[[Note Name|Display Text]]
```
**Example:** `[[My First Note|Click here to read]]` shows "Click here to read" but links to "My First Note".

### Link to a Heading
```markdown
[[Note Name#Heading Name]]
```
**Example:** `[[My First Note#Getting Started]]` links directly to the "Getting Started" heading.

### Link to a Block
First, add a block ID:
```markdown
This is an important paragraph. ^important-block
```
Then link to it:
```markdown
[[My First Note#^important-block]]
```

### External Links
```markdown
[Google](https://www.google.com)
```

### Backlinks
- **Backlinks panel**: Click the **backlinks icon** in the right sidebar to see all notes that link TO the current note.
- Unlinked mentions: Obsidian also shows mentions of the note's name even without an explicit `[[link]]`.

**Example:** If `Note A` contains `[[Note B]]`, then opening `Note B` will show `Note A` in its backlinks panel.

### Aliases
You can give a note multiple names in its properties:
```yaml
---
aliases:
  - AI
  - Machine Learning
---
```
Now `[[AI]]` and `[[Machine Learning]]` both link to this note.

---

## 4. Tags

Tags help you categorize and find notes.

### Basic Tags
```markdown
#productivity
#learning/obsidian
#project/active
```

### Nested Tags
```markdown
#book/fiction
#book/nonfiction/science
```
Nested tags create a hierarchy. Searching `#book` finds all notes tagged with `#book`, `#book/fiction`, etc.

### Where to Use Tags
- **Inline**: Write tags anywhere in the body of a note
- **Frontmatter**: Add to the properties section
```yaml
---
tags:
  - productivity
  - obsidian
---
```

### Tag Pane
- Open the **Tag pane** from the right sidebar to see all tags in your vault
- Click any tag to filter notes

---

## 5. Folders & File Management

### Creating Folders
1. Right-click in the **File Explorer** (left sidebar)
2. Select **"New folder"**
3. Name your folder

### Moving Notes
- **Drag and drop** in the File Explorer
- Right-click → **"Move file to..."**

### Renaming Notes
- Right-click → **"Rename"** or click the file name and press `F2`
- All links to the renamed note update automatically!

### Sorting Files
- Click the sort icon at the top of the File Explorer
- Options: File name (A→Z), Modified time, Created time

### Example Folder Structure
```
📁 My Vault
├── 📁 Projects
│   ├── 📁 Active
│   └── 📁 Archive
├── 📁 Areas
│   ├── 📁 Health
│   └── 📁 Finance
├── 📁 Resources
│   └── 📁 Books
└── 📁 Daily Notes
```

---

## 6. Search

### Basic Search
Press `Ctrl/Cmd + Shift + F` to open the global search.

### Search Operators

| Operator | Example | Finds |
|----------|---------|-------|
| `""` | `"exact phrase"` | Notes containing the exact phrase |
| `path:` | `path:Projects` | Notes in the Projects folder |
| `file:` | `file:meeting` | Notes with "meeting" in the filename |
| `tag:` | `tag:important` | Notes with the #important tag |
| `line:` | `line:(task deadline)` | Notes with both words on the same line |
| `section:` | `section:(## Summary)` | Content under a specific heading |
| `block:` | `block:(idea important)` | Both words in the same block/paragraph |
| `-` | `-tag:archive` | Exclude notes with #archive tag |
| `OR` | `cat OR dog` | Notes containing either word |

### Search Examples
```
tag:project path:Active -tag:completed
```
Finds all notes tagged `#project` in the `Active` folder that are NOT tagged `#completed`.

```
"meeting notes" file:2024
```
Finds notes with "meeting notes" in content and "2024" in the filename.

---

## 7. Graph View

The Graph View shows a **visual map** of all your notes and how they're connected.

### Opening Graph View
- Click the **Graph View icon** in the left sidebar
- Or use the Command Palette: `Ctrl/Cmd + P` → type "Graph view"

### Controls
- **Zoom**: Scroll wheel
- **Pan**: Click and drag background
- **Focus**: Click a node to highlight its connections
- **Open note**: Double-click a node

### Filters
In the Graph View settings (gear icon):
- **Search filter**: Show only matching notes (e.g., `path:Projects`)
- **Tags**: Color-code nodes by tag
- **Attachments**: Show/hide attachment files
- **Existing files only**: Hide links to notes that don't exist yet
- **Orphans**: Show/hide notes with no links

### Local Graph
- Open a note, then open its **Local Graph** from the "More options" menu (⋮)
- Shows only the direct connections of that specific note

### Example Use Case
If you have notes on `[[Python]]`, `[[Data Science]]`, and `[[Machine Learning]]`, and they all link to each other, the Graph View will show a cluster — revealing that these topics are closely related in your knowledge base.

---

## 8. Canvas

Canvas is Obsidian's **infinite whiteboard** for visual thinking.

### Creating a Canvas
1. `Ctrl/Cmd + P` → "Create new canvas"
2. Or right-click a folder → "New canvas"

### Canvas Elements

**Cards (text cards):**
- Double-click empty space to create a card
- Write markdown inside cards

**Notes (embedded notes):**
- Drag a note from the File Explorer onto the canvas
- The note content appears live

**Media:**
- Drag images, PDFs, or other files onto the canvas

**Links (from web):**
- Paste a URL to embed a web page

### Connections
- Hover over a card edge → drag the arrow to another card
- This creates a visual connection/arrow

### Example Canvas Layout
```
[Research Topic] ──→ [Key Findings] ──→ [Action Items]
       │                    │
       ▼                    ▼
[Source Notes]       [Questions to Explore]
```

---

## 9. Templates

Templates let you **insert pre-made content** into notes.

### Setting Up Templates
1. Create a folder called `Templates` (or any name)
2. Go to **Settings → Core Plugins → Templates**
3. Set the **Template folder location** to your folder

### Creating a Template
Create a note in your Templates folder:

**Example: Meeting Notes Template**
```markdown
## Meeting: {{title}}
**Date:** {{date}}
**Time:** {{time}}
**Attendees:**
- 

## Agenda
1. 

## Discussion Notes


## Action Items
- [ ] 

## Next Steps

```

### Template Variables
| Variable | Output Example |
|----------|---------------|
| `{{title}}` | The note's title |
| `{{date}}` | Today's date (e.g., 2024-01-15) |
| `{{time}}` | Current time (e.g., 14:30) |
| `{{date:YYYY-MM-DD}}` | Custom date format |
| `{{time:HH:mm}}` | Custom time format |

### Inserting a Template
1. Open a note where you want the template
2. `Ctrl/Cmd + P` → "Insert template"
3. Select your template
4. Content is inserted with variables replaced

---

## 10. Daily Notes

Daily Notes create a **new note for each day** automatically.

### Enabling Daily Notes
1. **Settings → Core Plugins → Daily notes** → Toggle ON

### Configuration
- **Date format**: `YYYY-MM-DD` (default) or customize (e.g., `YYYY/MM/MMMM/YYYY-MM-DD`)
- **New file location**: Set a folder (e.g., `Daily Notes`)
- **Template**: Select a template for new daily notes

### Daily Note Template Example
```markdown
# {{date:dddd, MMMM D, YYYY}}

## 🎯 Today's Goals
- [ ] 
- [ ] 
- [ ] 

## 📝 Notes


## 📖 What I Learned


## 🙏 Gratitude
1. 
2. 
3. 

## ✅ End of Day Review
- What went well?
- What could improve?
```

### Opening Today's Daily Note
- Click the **calendar icon** in the left sidebar
- Or `Ctrl/Cmd + P` → "Open today's daily note"

---

## 11. Outline

The Outline panel shows a **table of contents** based on headings in the current note.

### Opening Outline
- Click the **Outline icon** in the right sidebar
- Or `Ctrl/Cmd + P` → "Show outline"

### Usage
- Click any heading to jump to that section
- Works with all heading levels (`#` through `######`)

### Example
If your note has:
```markdown
# Introduction
## Background
## Problem Statement
# Methods
## Data Collection
## Analysis
# Results
# Conclusion
```
The Outline panel will show a nested tree you can click to navigate.

---

## 12. Bookmarks (Starred)

Bookmark your most important or frequently accessed notes.

### How to Bookmark
- Right-click a note → **"Bookmark"**
- Or open a note → click the bookmark icon in the tab bar
- You can also bookmark: **searches**, **headings**, **blocks**, and **graphs**

### Accessing Bookmarks
- Click the **Bookmark icon** (🔖) in the left sidebar
- All bookmarked items appear in a list

### Bookmark Groups
- Create groups to organize bookmarks (e.g., "Active Projects", "References")
- Drag bookmarks between groups

---

## 13. Command Palette

The Command Palette gives you **quick access to every action** in Obsidian.

### Opening
- Press `Ctrl/Cmd + P`

### Usage Examples
| Type this... | To do this... |
|-------------|---------------|
| `new note` | Create a new note |
| `toggle` | Toggle various features (checkbox, bold, etc.) |
| `graph` | Open graph view |
| `split` | Split the editor pane |
| `theme` | Change theme |
| `template` | Insert a template |
| `export` | Export to PDF |

### Pinned Commands
- You can pin frequently used commands to appear at the top of the palette

---

## 14. Quick Switcher

Quickly **open any note** by typing part of its name.

### Opening
- Press `Ctrl/Cmd + O`

### Features
- Fuzzy search: type `mtnts` to find "Meeting Notes"
- Shows recent files at the top
- Press `Enter` to open the selected note
- Press `Ctrl/Cmd + Enter` to open in a new pane

### Example
Instead of browsing through folders:
1. Press `Ctrl/Cmd + O`
2. Type `proj` 
3. Select "Project Plan 2024" from results
4. Press `Enter`

---

## 15. Properties (Frontmatter / YAML)

Properties add **structured metadata** to your notes using YAML frontmatter.

### Basic Syntax
```yaml
---
title: My Research Paper
date: 2024-01-15
status: in-progress
tags:
  - research
  - science
category: Academic
rating: 4
---
```

### Property Types
| Type | Example |
|------|---------|
| Text | `author: John Doe` |
| Number | `rating: 5` |
| Date | `created: 2024-01-15` |
| Checkbox | `published: true` |
| List | `tags: [a, b, c]` |
| Link | `related: "[[Other Note]]"` |

### Using Properties
- Properties appear at the top of each note in **Properties view**
- Search by properties: `[property:value]` in search
- Community plugins (like Dataview) can query properties powerfully

### Example: Book Note with Properties
```yaml
---
title: Atomic Habits
author: James Clear
rating: 5
status: finished
date-read: 2024-01-10
tags:
  - book
  - self-improvement
  - habits
---

# Atomic Habits - Notes

## Key Takeaways
- Habits are the compound interest of self-improvement
- Focus on systems, not goals
```

---

## 16. Callouts

Callouts are **styled blocks** for highlighting important information.

### Basic Syntax
```markdown
> [!note]
> This is a note callout. Use it for general information.

> [!tip]
> This is a helpful tip!

> [!warning]
> Be careful about this!

> [!danger]
> Critical warning — do not ignore!
```

### All Callout Types
| Type | Icon | Use For |
|------|------|---------|
| `note` | 📝 | General information |
| `abstract` / `summary` | 📋 | Summaries |
| `info` | ℹ️ | Informational |
| `tip` / `hint` | 🔥 | Helpful tips |
| `success` / `check` | ✅ | Positive outcomes |
| `question` / `faq` | ❓ | Questions |
| `warning` / `caution` | ⚠️ | Warnings |
| `failure` / `fail` | ❌ | Failures |
| `danger` / `error` | 🚫 | Critical warnings |
| `bug` | 🐛 | Bug reports |
| `example` | 📖 | Examples |
| `quote` / `cite` | 💬 | Quotations |

### Custom Title
```markdown
> [!tip] My Custom Title
> The content goes here.
```

### Foldable Callouts
```markdown
> [!faq]- Click to expand (collapsed by default)
> This content is hidden until clicked.

> [!faq]+ Click to collapse (expanded by default)
> This content is shown by default.
```

### Nested Callouts
```markdown
> [!question] Can callouts be nested?
> > [!success] Yes they can!
> > Like this.
```

---

## 17. Embeds & Transclusion

Embed content from other notes **directly into the current note**.

### Embed an Entire Note
```markdown
![[Note Name]]
```

### Embed a Heading Section
```markdown
![[Note Name#Heading]]
```

### Embed a Block
```markdown
![[Note Name#^block-id]]
```

### Embed an Image
```markdown
![[image.png]]
![[image.png|400]]  <!-- with width -->
```

### Embed a PDF
```markdown
![[document.pdf]]
![[document.pdf#page=5]]  <!-- specific page -->
```

### Embed Audio/Video
```markdown
![[recording.mp3]]
![[video.mp4]]
```

### Example: Dashboard Note with Embeds
```markdown
# My Dashboard

## Current Projects
![[Active Projects#In Progress]]

## Today's Tasks
![[2024-01-15#Tasks]]

## Quick Reference
![[Keyboard Shortcuts#Most Used]]
```

---

## 18. Split Panes & Workspaces

### Splitting Panes
- **Right split**: Right-click a tab → "Split right"
- **Down split**: Right-click a tab → "Split down"
- **Drag tabs**: Drag a tab to any edge to split

### Tab Groups
- Multiple tabs within each pane
- Drag tabs between pane groups

### Linked Views
- Right-click a tab → "Open linked view" → choose "Backlinks", "Outline", "Local Graph"
- The linked view follows the active note in that pane

### Workspaces
Save and load **different pane layouts**:
1. Enable **Workspaces** core plugin
2. `Ctrl/Cmd + P` → "Manage workspaces"
3. Save current layout with a name (e.g., "Writing", "Research", "Review")
4. Switch between saved workspaces

### Example Workspace: Research
```
┌─────────────────┬─────────────────┐
│  Source Note     │  My Notes       │
│  (Reading)       │  (Writing)      │
├─────────────────┼─────────────────┤
│  Graph View      │  Outline        │
│  (Local)         │  (Navigation)   │
└─────────────────┴─────────────────┘
```

---

## 19. Appearance & Themes

### Changing Themes
1. **Settings → Appearance → Themes**
2. Click **"Manage"** to browse community themes
3. Popular themes: Minimal, Things, Blue Topaz, AnuPpuccin

### Base Color Scheme
- **Settings → Appearance** → Choose Light or Dark mode
- Or `Ctrl/Cmd + P` → "Toggle light/dark mode"

### Font Settings
- **Settings → Appearance**:
  - Interface font
  - Text (editor) font
  - Monospace (code) font
  - Font size

### CSS Snippets (Custom Styling)
1. **Settings → Appearance → CSS snippets** → Open folder
2. Create a `.css` file
3. Toggle it on in Settings

**Example snippet** (`custom-headers.css`):
```css
/* Make H1 headings centered with a bottom border */
.markdown-preview-view h1 {
  text-align: center;
  border-bottom: 2px solid var(--text-accent);
  padding-bottom: 10px;
}
```

---

## 20. Hotkeys (Keyboard Shortcuts)

### Essential Shortcuts
| Action | Windows/Linux | Mac |
|--------|--------------|-----|
| New note | `Ctrl + N` | `Cmd + N` |
| Open Quick Switcher | `Ctrl + O` | `Cmd + O` |
| Command Palette | `Ctrl + P` | `Cmd + P` |
| Search in vault | `Ctrl + Shift + F` | `Cmd + Shift + F` |
| Search in file | `Ctrl + F` | `Cmd + F` |
| Toggle edit/preview | `Ctrl + E` | `Cmd + E` |
| Bold | `Ctrl + B` | `Cmd + B` |
| Italic | `Ctrl + I` | `Cmd + I` |
| Insert link | `Ctrl + K` | `Cmd + K` |
| Toggle checkbox | `Ctrl + Enter` | `Cmd + Enter` |
| Indent | `Tab` | `Tab` |
| Unindent | `Shift + Tab` | `Shift + Tab` |
| Close current tab | `Ctrl + W` | `Cmd + W` |
| Open settings | `Ctrl + ,` | `Cmd + ,` |

### Customizing Hotkeys
1. **Settings → Hotkeys**
2. Search for any command
3. Click the `+` icon to assign a new shortcut
4. Click the `×` to remove an existing shortcut

---

## 21. Core Plugins Overview

Obsidian includes these **built-in plugins** (toggle in Settings → Core Plugins):

| Plugin | Description |
|--------|-------------|
| **Audio recorder** | Record audio directly in Obsidian |
| **Backlinks** | Show links pointing to current note |
| **Bookmarks** | Star important notes, searches, and more |
| **Canvas** | Infinite whiteboard |
| **Command palette** | Quick access to commands |
| **Daily notes** | Create a note for each day |
| **File recovery** | Restore previous versions of notes |
| **Files** | File explorer sidebar |
| **Format converter** | Convert from other formats |
| **Graph view** | Visual note connections |
| **Note composer** | Merge and split notes |
| **Outgoing links** | Show links from current note |
| **Outline** | Table of contents for current note |
| **Page preview** | Hover preview of linked notes |
| **Properties view** | Edit note properties |
| **Publish** | Publish notes to the web |
| **Quick switcher** | Quickly open notes by name |
| **Random note** | Open a random note |
| **Search** | Full-text search |
| **Slash commands** | Type `/` to insert elements |
| **Slides** | Present notes as slideshows |
| **Sync** | Sync between devices |
| **Tags view** | Browse all tags |
| **Templates** | Insert templates |
| **Unique note creator** | Create Zettelkasten-style notes |
| **Word count** | Show word/character count |
| **Workspaces** | Save and load workspace layouts |

---

## 22. Community Plugins

### Enabling Community Plugins
1. **Settings → Community Plugins**
2. Turn off **Restricted Mode**
3. Click **Browse** to find plugins

### Must-Have Plugins for Beginners

| Plugin | What It Does |
|--------|-------------|
| **Calendar** | Calendar view for daily notes |
| **Dataview** | Query your notes like a database |
| **Templater** | Advanced templates with logic |
| **Excalidraw** | Drawing and diagramming |
| **Kanban** | Kanban boards for task management |
| **Style Settings** | Fine-tune theme appearance |
| **Periodic Notes** | Weekly/monthly/yearly notes |
| **Advanced Tables** | Better table editing |
| **Linter** | Auto-format your notes |
| **Iconize** | Add icons to folders and files |

### Installing a Plugin
1. **Settings → Community Plugins → Browse**
2. Search for the plugin name
3. Click **Install**
4. Click **Enable**
5. Configure in **Settings → Community Plugins → (Plugin Name)**

### Dataview Example
After installing Dataview, you can query your notes:
````markdown
```dataview
TABLE author, rating, status
FROM #book
SORT rating DESC
```
````
This creates a table of all notes tagged `#book` showing their author, rating, and status properties.

---

## 23. Obsidian Sync

Obsidian's official **end-to-end encrypted sync service**.

### Features
- Sync across all devices (desktop, mobile, tablet)
- End-to-end encryption
- Version history (up to 12 months)
- Selective folder sync
- Sync settings and plugins

### Setup
1. **Settings → Sync**
2. Log in to your Obsidian account
3. Create or connect a remote vault
4. Choose what to sync (notes, images, plugins, settings, themes)

### Alternative Free Sync Methods
- **iCloud**: Put vault in iCloud Drive (Mac/iOS)
- **Google Drive / Dropbox / OneDrive**: Put vault folder in sync folder
- **Git**: Use the Obsidian Git community plugin
- **Syncthing**: Free, open-source peer-to-peer sync

---

## 24. Obsidian Publish

Publish your notes as a **website**.

### Features
- Custom domain support
- Password protection
- Graph view on the web
- Theme customization
- Selective publishing (choose which notes to publish)

### Setup
1. Enable **Publish** core plugin
2. **Settings → Publish**
3. Sign in and create a site
4. Select notes to publish
5. Click **Publish changes**

### Alternatives for Free Publishing
- **Quartz**: Free, open-source static site generator for Obsidian
- **Digital Garden plugin**: Publish to a free Netlify/Vercel site
- **MkDocs**: Convert vault to a documentation site

---

## 25. File Recovery & Version History

### File Recovery (Core Plugin)
- **Settings → Core Plugins → File recovery** → Enable
- Obsidian automatically takes snapshots of your notes
- **Settings → File Recovery** → Set snapshot interval and history length

### Recovering a Previous Version
1. `Ctrl/Cmd + P` → "Show file recovery"
2. Browse snapshots by date/time
3. Click a snapshot to preview
4. Click **"Restore"** to revert

### With Obsidian Sync
- Full version history for synced notes (up to 12 months)
- View and restore any previous version

---

## 26. Slash Commands

Type `/` in the editor to quickly insert elements.

### Enable
**Settings → Core Plugins → Slash commands** → Toggle ON

### Available Slash Commands
| Command | Inserts |
|---------|---------|
| `/heading` | A heading (choose level) |
| `/table` | A markdown table |
| `/callout` | A callout block |
| `/code block` | A code block |
| `/link` | An internal link |
| `/embed` | An embedded note |
| `/template` | A template |
| `/todo` | A checkbox item |
| `/divider` | A horizontal rule |
| `/date` | Today's date |

---

## 27. Audio Recorder

Record audio directly within Obsidian.

### Enable
**Settings → Core Plugins → Audio recorder** → Toggle ON

### Usage
1. `Ctrl/Cmd + P` → "Start recording"
2. Speak your notes
3. `Ctrl/Cmd + P` → "Stop recording"
4. Audio file is saved and embedded in the current note

### Output
```markdown
![[Recording 20240115143022.webm]]
```

---

## 28. Word Count & Reading Time

### Enable
**Settings → Core Plugins → Word count** → Toggle ON

### What It Shows
- Word count
- Character count
- Reading time estimate

Displayed in the **status bar** at the bottom of the editor.

### Selecting Text
- Select a portion of text to see the word count of just the selection

---

## 29. Vim Mode (Editor)

For users familiar with Vim keybindings.

### Enable
**Settings → Editor → Vim key bindings** → Toggle ON

### Basic Vim Commands in Obsidian
| Mode | Key | Action |
|------|-----|--------|
| Normal | `i` | Enter Insert mode |
| Normal | `v` | Enter Visual mode |
| Normal | `dd` | Delete line |
| Normal | `yy` | Copy (yank) line |
| Normal | `p` | Paste |
| Normal | `/` | Search |
| Normal | `u` | Undo |
| Normal | `Ctrl + r` | Redo |
| Insert | `Esc` | Back to Normal mode |

---

## 30. Tips & Best Practices

### For Beginners
1. **Start simple**: Don't try to set up a complex system right away
2. **Write first, organize later**: Focus on capturing ideas
3. **Link liberally**: Create `[[links]]` whenever one note relates to another
4. **Use daily notes**: Build a journaling habit
5. **Learn one feature at a time**: Don't overwhelm yourself

### Note-Taking Methods
| Method | Description | Good For |
|--------|-------------|----------|
| **Zettelkasten** | Atomic notes connected by links | Research, knowledge building |
| **PARA** | Projects, Areas, Resources, Archive folders | Task management |
| **MOCs** | Maps of Content — index notes linking to related notes | Navigation, overviews |
| **Daily Notes + Links** | Daily journal with links to topic notes | Journaling, logging |

### Map of Content (MOC) Example
```markdown
# Programming MOC

## Languages
- [[Python]]
- [[JavaScript]]
- [[Rust]]

## Concepts
- [[Data Structures]]
- [[Algorithms]]
- [[Design Patterns]]

## Projects
- [[Web App Project]]
- [[CLI Tool Project]]
```

### Useful Tricks
- **Hover preview**: Hold `Ctrl/Cmd` and hover over a link to preview the note
- **Star important notes**: Use Bookmarks for quick access
- **Use headings consistently**: Makes Outline navigation and embedding easier
- **Create a Home note**: A starting point dashboard linking to your main areas
- **Regular review**: Periodically review and link orphan notes

---

## Summary

Obsidian is as simple or as powerful as you make it. Start with basic notes and links, then gradually explore features like templates, graph view, and community plugins as you grow comfortable.

The key principle: **Your notes are plain text files that you own forever.** Build your second brain one note at a time.

---

*Happy note-taking! 📝*
