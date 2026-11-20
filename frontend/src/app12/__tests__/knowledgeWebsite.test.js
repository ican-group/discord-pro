/** Restoration R1/R2: website knowledge sources (add, status, refresh, imported pages, content). */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { websiteProblem } from "../components/Knowledge";
import { IDS, MockApi, SAMPLES, renderApp, withPermissions } from "../testing/harness";

const WEB = SAMPLES.knowledge_sources_website.body;
const PAGES = SAMPLES.knowledge_documents_website.body;
const SRC = WEB.sources[0];

function orgApi() {
  const api = new MockApi();
  api.on("GET /{type_}/{object_id}/config", withPermissions({ edit_config: true, edit_policy: true }));
  api.on("GET /{owner_type}/{owner_id}/knowledge/sources", { status: 200, body: WEB });
  api.on("GET /knowledge/sources/{source_id}/documents", { status: 200, body: PAGES });
  return api;
}

test("the add-website form is validated before anything is sent", () => {
  expect(websiteProblem({ name: "", url: "https://x.example" })).toMatch(/name/);
  expect(websiteProblem({ name: "Help", url: "" })).toMatch(/address/);
  expect(websiteProblem({ name: "Help", url: "ftp://x.example" })).toMatch(/http/);
  expect(websiteProblem({ name: "Help", url: "x.example", mode: "crawl", maxPages: "500" })).toMatch(/between 1 and 200/);
  expect(websiteProblem({ name: "Help", url: "docs.example.com/help", mode: "single" })).toBe("");
});

test("an admin adds a website (single page or crawl with a page limit)", async () => {
  const api = orgApi();
  api.on("POST /{owner_type}/{owner_id}/knowledge/sources", { status: 201, body: SRC });
  renderApp(`/app/organisations/${IDS.organisation}`, api);
  await userEvent.click(await screen.findByRole("tab", { name: "Knowledge" }));
  const form = await screen.findByRole("form", { name: "Add website" });
  await userEvent.type(within(form).getByLabelText("Website address"), "https://docs.example.com/help");
  await userEvent.type(within(form).getByLabelText("Source name"), "Help site");
  await userEvent.selectOptions(within(form).getByLabelText("Import"), "crawl");
  const limit = within(form).getByLabelText("Page limit");
  await userEvent.clear(limit);
  await userEvent.type(limit, "40");
  await userEvent.click(within(form).getByRole("button", { name: "Add website" }));
  await waitFor(() => expect(api.calls("POST /{owner_type}/{owner_id}/knowledge/sources")[0].body).toEqual({
    name: "Help site", website: { url: "https://docs.example.com/help", mode: "crawl", max_pages: 40 } }));
  expect(await screen.findByText(/import runs in the background/)).toBeInTheDocument();
});

test("status, last fetch, errors, refresh and imported page content are shown", async () => {
  const api = orgApi();
  const failed = { ...SRC, website: { ...SRC.website, state: "failed", error: "The page was not found (HTTP 404)." } };
  api.on("GET /{owner_type}/{owner_id}/knowledge/sources", { status: 200, body: { sources: [failed] } });
  api.on("POST /knowledge/sources/{source_id}/refresh", { status: 200, body: { ...SRC, website: { ...SRC.website, state: "queued" } } });
  api.on("GET /knowledge/documents/{document_id}/content", { status: 200,
    body: { id: PAGES.documents[0].id, text: "Help. Opening hours are 9 to 5.", truncated: false } });
  renderApp(`/app/organisations/${IDS.organisation}`, api);
  await userEvent.click(await screen.findByRole("tab", { name: "Knowledge" }));
  expect(await screen.findByText("The page was not found (HTTP 404).")).toBeInTheDocument();
  expect(screen.getByText(/Last successful fetch:/)).toBeInTheDocument();
  expect(screen.getAllByText(SRC.website.url).length).toBeGreaterThan(0);
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(api.calls("POST /knowledge/sources/{source_id}/refresh")).toHaveLength(1));
  await userEvent.click(screen.getByRole("button", { name: "Pages" }));
  await userEvent.click(await screen.findByRole("button", { name: "View content" }));
  expect(await screen.findByText("Help. Opening hours are 9 to 5.")).toBeInTheDocument();
  // website sources are filled from the site: no upload form
  expect(screen.queryByLabelText(/Upload a document/)).toBeNull();
});
