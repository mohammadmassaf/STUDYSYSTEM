-- Nothing in a note runs or loads when it prints (D-81). A note body is model-written, and the
-- page is opened by Chrome to print it: a <script> must never run, and no file or URL may be
-- pulled in - pandoc's --embed-resources would read a local path a markdown image names, or
-- fetch a remote one. So raw HTML becomes text (pandoc's GFM reader ignores `-raw_html`), and
-- an image becomes `[image: its alt text]`. A link keeps its address only when it opens a web
-- page, an email or a place in the note: in a PDF, a click on `file://host/share` sends the
-- Windows login, and `javascript:` or `ms-msdt:` runs something.

function RawBlock(el)
  if el.format:match("html") then return pandoc.Para({ pandoc.Str(el.text) }) end
end

function RawInline(el)
  if el.format:match("html") then return pandoc.Str(el.text) end
end

local SAFE = { http = true, https = true, mailto = true }

function Link(el)
  if el.target:sub(1, 1) == "#" then return nil end
  local scheme = el.target:match("^(%a[%w+.-]*):")
  if scheme and SAFE[scheme:lower()] then return nil end
  return el.content
end

function Image(el)
  return pandoc.Str("[image: " .. pandoc.utils.stringify(el.caption) .. "]")
end
