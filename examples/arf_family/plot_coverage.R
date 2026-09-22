# Response-size figure for the ARF dossier's 16 chain tools.
#
# A calls-per-tool figure over all 50 tools carries no information: the
# 34 tools outside the chain were never called and the 16 inside it are
# called by a fixed rule (once per locus, or once per organism through a
# batch_ form). `coverage.tsv` still has a row for all 50 (built by
# `coverage.py`); this figure instead plots the one thing that DOES vary
# per call — response size — from `calls.jsonl` directly rather than from
# the aggregated table. `n_bytes` there is the
# length of the JSON-RPC response LINE, which carries each payload as both
# `content[].text` and `structuredContent` — a transport figure, not the
# size of the tool's own payload, so every size label on this figure says
# "on the wire".
#
# Colour (fill) is a per-TOOL property, `release_status` from
# `coverage.tsv` — computed in coverage.py from calls.jsonl plus
# gaps.jsonl's "version-under-another-key" row, never re-derived here —
# so all of a tool's points share one fill. Two properties this figure
# must keep: (1) the colour is the three-level table column, because a
# two-level "reports a release / does not" would assert an absence that
# gaps.jsonl's own row contradicts for the tools that report one under
# another key; (2) every position is deterministic — no jitter — so the
# same log draws the same figure and the page's counts can be checked
# against it.
#
# One row of calls.jsonl is one MCP call. Eight chain tools go through
# their batch_ form (one call per organism per 50 loci); the other eight
# are called once per locus, so a per-locus tool draws up to a point per
# locus per organism and a batch tool draws one per organism. The vertical
# offset is per ORGANISM, not per gene: one point per gene per tool is not
# readable at this count, and the organism is what the eye has to
# separate. The shape legend and the caption both name the organism
# encoding. Calls of kind "expected" (a documented organism refusal,
# e.g. kegg_pathways on wheat) are drawn hollow: they are answers, not
# data.

library(ggplot2)
library(jsonlite)

source("examples/arf_family/theme_pgmcp.R")

cov <- read.delim("examples/arf_family/coverage.tsv")
chain <- cov[cov$status != "unused", ]

calls <- jsonlite::stream_in(file("examples/arf_family/calls.jsonl"), verbose = FALSE)
calls <- calls[calls$tool %in% chain$tool, ]

tool_order <- chain$tool[order(chain$median_bytes)]
calls$tool <- factor(calls$tool, levels = tool_order)

release_lookup <- setNames(chain$release_status, chain$tool)
release_levels <- c("upstream_version field", "release under another key", "no release in the payload")
calls$release_status <- factor(release_lookup[as.character(calls$tool)], levels = release_levels)

# Deterministic per-organism vertical offset, by sorted organism slug —
# replaces random jitter, which drew a different figure on every render
# and still overlapped exact ties.
organisms_sorted <- sort(unique(calls$organism))
stopifnot(length(organisms_sorted) <= 3)
organism_offset <- setNames(c(-0.25, 0, 0.25)[seq_along(organisms_sorted)], organisms_sorted)
calls$y <- as.numeric(calls$tool) + organism_offset[calls$organism]
# Exact ties inside one organism (the same tool answering the same byte
# count on several calls; the largest group here is 20) draw on ONE point.
# A glyph is about a third of a row's height, so a 20-way tie cannot be
# separated inside its row, and pushing it out of the row would
# misattribute the tool. The caption says so and PAGE.md gives the count
# for each tie it relies on.
calls$organism <- factor(calls$organism, levels = organisms_sorted)
if (is.null(calls$kind)) calls$kind <- ifelse(calls$ok, "ok", "error")
calls$expected <- calls$kind == "expected"

n_calls <- nrow(calls)
n_genes <- nrow(read.delim("examples/arf_family/genes.tsv"))

oversize_threshold <- 200000  # bytes; the dossier runner's gaps_auto.jsonl cutoff

min_bytes <- min(calls$n_bytes)
max_bytes <- max(calls$n_bytes)
fold <- round(max_bytes / min_bytes)
n_upstream_field <- sum(chain$release_status == "upstream_version field")
n_chain <- nrow(chain)

title_line1 <- sprintf(
  "Response sizes on the wire span %s×, from %s B to %s kB",
  fold, format(min_bytes, big.mark = ","), round(max_bytes / 1000)
)
title_line2 <- sprintf(
  "%d of %d tools called name the release under upstream_version",
  n_upstream_field, n_chain
)

p <- ggplot(calls, aes(x = n_bytes, y = y)) +
  geom_vline(
    aes(xintercept = oversize_threshold, linetype = "200 kB oversize threshold"),
    colour = pgmcp_refline_colour
  ) +
  geom_point(
    data = calls[!calls$expected, ],
    aes(fill = release_status, shape = organism),
    colour = pgmcp_point_outline,
    stroke = pgmcp_point_stroke,
    size = pgmcp_point_size,
    alpha = 0.85
  ) +
  geom_point(
    data = calls[calls$expected, ],
    aes(shape = organism),
    fill = NA,
    colour = pgmcp_refline_colour,
    stroke = pgmcp_point_stroke,
    size = pgmcp_point_size
  ) +
  scale_shape_manual(
    name = NULL,
    values = c(21, 22, 24)[seq_along(organisms_sorted)],
    breaks = organisms_sorted,
    labels = gsub("_", " ", organisms_sorted),
    guide = ggplot2::guide_legend(override.aes = list(fill = "grey60"))
  ) +
  scale_x_log10(labels = scales::label_comma()) +
  scale_y_continuous(
    breaks = seq_along(levels(calls$tool)),
    labels = levels(calls$tool),
    limits = c(0.5, length(levels(calls$tool)) + 0.5),
    expand = c(0, 0)
  ) +
  scale_fill_manual(
    values = pal_pgmcp, breaks = release_levels,
    # Two rows: the three release_status labels together don't fit one row
    # at the figure's fixed 7in width without clipping ("no release in the
    # payload" truncates off the right edge in one row). With `shape`
    # mapped to organism the fill keys draw no glyph unless a shape is
    # forced onto them.
    guide = ggplot2::guide_legend(nrow = 2, override.aes = list(shape = 21))
  ) +
  scale_linetype_manual(name = NULL, values = c("200 kB oversize threshold" = "dashed")) +
  labs(
    title = paste0(title_line1, "\n", title_line2),
    x = "response size on the wire, bytes (log10 scale)",
    y = NULL,
    fill = NULL,
    caption = sprintf(
      paste0(
        "%d calls, %d genes, %d organisms; a batch_* call covers up to 50 loci. Tools ordered by median size.\n",
        "One point per distinct (organism, size) per tool; hollow = documented refusal."
      ),
      n_calls, n_genes, length(organisms_sorted)
    )
  ) +
  theme_pgmcp()

ggsave("examples/arf_family/coverage.png", p, width = 7, height = 6, dpi = 200)
