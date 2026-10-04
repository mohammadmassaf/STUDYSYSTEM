-- Nothing in a note runs or loads when it prints (D-81). A note body is model-written, and the
-- page is opened by Chrome to print it: a <script> must never run, and no file or URL may be
-- pulled in - pandoc's --embed-resources would read a local path a markdown image names, or
-- fetch a remote one. So raw HTML becomes text (pandoc's GFM reader ignores `-raw_html`), and
-- an image becomes `[image: its alt text]`.

function RawBlock(el)
  if el.format:match("html") then return pandoc.Para({ pandoc.Str(el.text) }) end
end

function RawInline(el)
  if el.format:match("html") then return pandoc.Str(el.text) end
end

function Image(el)
  return pandoc.Str("[image: " .. pandoc.utils.stringify(el.caption) .. "]")
end
