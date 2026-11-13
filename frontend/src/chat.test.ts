import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { getMessageText, MarkdownRenderer, parseMarkdownBlocks } from "./Chat";

describe("parseMarkdownBlocks", () => {
  it("returns single text block for plain text", () => {
    const blocks = parseMarkdownBlocks("Hello world");
    expect(blocks).toEqual([{ id: "text-0", type: "text", content: "Hello world" }]);
  });

  it("handles standard code block with LF line endings", () => {
    const markdown = "Here is code:\n```python\nprint('hello')\n```\nDone.";
    const blocks = parseMarkdownBlocks(markdown);
    expect(blocks).toHaveLength(3);
    expect(blocks[0]).toMatchObject({ type: "text", content: "Here is code:\n" });
    expect(blocks[1]).toMatchObject({
      type: "code",
      language: "python",
      content: "print('hello')\n",
    });
    expect(blocks[2]).toMatchObject({ type: "text", content: "\nDone." });
  });

  it("handles code block with CRLF (Windows) line endings", () => {
    const markdown = "Intro\r\n```python\r\nprint('windows')\r\n```\r\nOutro";
    const blocks = parseMarkdownBlocks(markdown);
    expect(blocks).toHaveLength(3);
    expect(blocks[0]).toMatchObject({ type: "text", content: "Intro\r\n" });
    expect(blocks[1]).toMatchObject({
      type: "code",
      language: "python",
      content: "print('windows')\r\n",
    });
    expect(blocks[2]).toMatchObject({ type: "text", content: "\r\nOutro" });
  });

  it("handles code block with special characters and trailing spaces in language name", () => {
    const markdown = "```c++  \nint x = 1;\n```";
    const blocks = parseMarkdownBlocks(markdown);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      type: "code",
      language: "c++",
      content: "int x = 1;\n",
    });
  });

  it("handles code block with no language specified", () => {
    const markdown = "```\nraw text\n```";
    const blocks = parseMarkdownBlocks(markdown);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      type: "code",
      language: "text",
      content: "raw text\n",
    });
  });

  it("handles unclosed code block during active streaming", () => {
    const markdown = "Streaming:\n```typescript\nconst a = 1;";
    const blocks = parseMarkdownBlocks(markdown);
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({ type: "text", content: "Streaming:\n" });
    expect(blocks[1]).toMatchObject({
      type: "code",
      language: "typescript",
      content: "const a = 1;",
    });
  });
});

describe("getMessageText", () => {
  it("extracts text from plain string", () => {
    expect(getMessageText("hello")).toBe("hello");
  });

  it("extracts text from array of text parts", () => {
    const parts = [
      { type: "text", text: "Hello " },
      { type: "text", text: "world" },
    ];
    expect(getMessageText(parts)).toBe("Hello world");
  });

  it("extracts text from array of raw strings", () => {
    expect(getMessageText(["foo", "bar"])).toBe("foobar");
  });

  it("handles empty or unexpected inputs safely", () => {
    expect(getMessageText(null)).toBe("");
    expect(getMessageText(undefined)).toBe("");
    expect(getMessageText(123)).toBe("");
    expect(getMessageText({})).toBe("");
    expect(getMessageText([{ other: "data" }])).toBe("");
  });
});

describe("MarkdownRenderer", () => {
  it("renders paragraphs and bold/italic text", () => {
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: "Hello **bold** and *italic*" }),
    );
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<em>italic</em>");
  });

  it("renders GFM markdown tables with headers and cells", () => {
    const tableMd = "| Column A | Column B |\n| --- | --- |\n| Cell 1 | Cell 2 |";
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: tableMd }),
    );
    expect(html).toContain("<table>");
    expect(html).toContain("<th>Column A</th>");
    expect(html).toContain("<td>Cell 1</td>");
  });

  it("renders external links with target=_blank and rel=noopener noreferrer", () => {
    const linkMd = "Visit [Google](https://google.com) today.";
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: linkMd }),
    );
    expect(html).toContain('href="https://google.com"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("renders inline code with inline-code class", () => {
    const codeMd = "Use the `fetch()` API.";
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: codeMd }),
    );
    expect(html).toContain('<code class="inline-code">fetch()</code>');
  });

  it("renders fenced code block with CodeBlock component", () => {
    const fencedMd = "```python\nprint('hello world')\n```";
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: fencedMd }),
    );
    expect(html).toContain('class="code-block"');
    expect(html).toContain('class="code-language"');
    expect(html).toContain("python");
    expect(html).toContain("print(&#x27;hello world&#x27;)");
  });

  it("wraps a real semantic table in a labelled scroll region", () => {
    const tableMd = "| Month | Orders |\n| --- | --- |\n| Jan | 29 |";
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: tableMd }),
    );
    expect(html).toContain(
      '<section class="table-scroll" aria-label="Table" tabindex="0">',
    );
    expect(html).toContain("<table>");
    expect(html).toContain("<th>Month</th>");
    expect(html).toContain("<td>Jan</td>");
    // The frame belongs to the intrinsic table, not an empty full-width block.
    expect(html).not.toContain('<div class="table-scroll"><table style');
  });

  it("renders image alt text instead of fetching a remote image", () => {
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, {
        content: "![Quarterly revenue](https://evil.test/tracker.png)",
      }),
    );
    expect(html).toContain("Quarterly revenue");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("evil.test");
  });

  it("keeps task lists and block quotes renderable", () => {
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, {
        content: "- [x] done\n- [ ] open\n\n> quoted line",
      }),
    );
    expect(html).toContain('type="checkbox"');
    expect(html).toContain("<blockquote>");
  });

  it("renders a partially streamed code fence without losing text", () => {
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, {
        content: "Streaming:\n```sql\nselect * from orders",
      }),
    );
    expect(html).toContain('class="code-block"');
    expect(html).toContain("select * from orders");
  });

  it("renders nested lists cleanly", () => {
    const listMd = "- Item 1\n  - Sub 1\n  - Sub 2\n- Item 2";
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: listMd }),
    );
    expect(html).toContain("<ul>");
    expect(html).toContain("<li>Item 1");
    expect(html).toContain("<li>Sub 1</li>");
  });

  it("escapes raw HTML safely without injecting elements", () => {
    const rawHtmlMd = "<script>alert('xss')</script>";
    const html = renderToStaticMarkup(
      createElement(MarkdownRenderer, { content: rawHtmlMd }),
    );
    expect(html).not.toContain("<script>");
  });
});
