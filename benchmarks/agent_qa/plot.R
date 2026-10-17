# Figures for the agent benchmark, from results.jsonl directly.
#
#   Rscript benchmarks/agent_qa/plot.R [results.jsonl] [outdir]
#
# Writes summary.tsv (verdicts, cost and time per setup; verdicts per setup x
# kind of question; the no-model baselines from baselines.jsonl), stats.tsv
# (bootstrap intervals over questions, and paired per-question tests), and
# three figures:
#   agent_qa.png             right / wrong / declined per setup (the README one)
#   agent_qa_cost_time.png   cost and seconds per answer, every run
#   agent_qa_by_category.png right / wrong / declined per setup x kind of question
# Bars count answers, not questions: 52 questions x 3 runs = 156 per setup.
# Every number on the figures is computed here from the log; nothing is typed
# in by hand. Green, red and grey mean right, wrong and declined in every
# figure; setups are told apart by their axis labels, not by colour.
# A tool run that never called its tools (valid = FALSE, see run.py)
# measured the model alone; it stays in the bars and is counted in the
# `invalid` column and the first figure's subtitle.

library(ggplot2)
library(jsonlite)

args <- commandArgs(trailingOnly = TRUE)
results_path <- if (length(args) >= 1) args[1] else "benchmarks/agent_qa/results.jsonl"
outdir <- if (length(args) >= 2) args[2] else "benchmarks/agent_qa"
source("examples/arf_family/theme_pgmcp.R")

r <- jsonlite::stream_in(file(results_path), verbose = FALSE)
stopifnot(all(r$verdict %in% names(pal_verdict)))
stopifnot(is.logical(r$valid), !anyNA(r$valid))
b <- jsonlite::stream_in(file(file.path(dirname(results_path), "baselines.jsonl")), verbose = FALSE)
b$valid <- TRUE
baseline_labels <- c(
  constant = "Baseline: one constant answer per kind",
  id_chromosome = "Baseline: chromosome read from the gene ID"
)
stopifnot(all(b$arm %in% names(baseline_labels)), all(b$verdict %in% names(pal_verdict)))

arm_labels <- c(
  bare = "Claude alone",
  web = "Claude + web search",
  mcp = "Claude + plant-genomics-mcp"
)
stopifnot(all(r$arm %in% names(arm_labels)))
r$arm_label <- factor(arm_labels[r$arm], levels = rev(arm_labels))
r$verdict <- factor(r$verdict, levels = names(pal_verdict))

cat_labels <- c(
  location = "Gene location",
  protein_length = "Protein length",
  symbol_to_locus = "Gene name → locus ID",
  uniprot_accession = "Locus → UniProt accession",
  ortholog = "One-to-one ortholog"
)
stopifnot(all(r$category %in% names(cat_labels)))

n_per_arm <- table(r$arm)
stopifnot(length(unique(n_per_arm)) == 1)
n_ans <- as.numeric(n_per_arm[1])
n_q <- length(unique(r$qid))
n_rep <- length(unique(r$rep))

# Tables first: the figures draw these numbers, and the page quotes them.
tally <- function(x) {
  data.frame(
    right = sum(x$verdict == "right"), wrong = sum(x$verdict == "wrong"),
    declined = sum(x$verdict == "declined"), no_answer = sum(x$verdict == "no_answer"),
    wrong_id = sum(x$wrong_id),
    invalid = sum(!x$valid), right_valid = sum(x$verdict == "right" & x$valid),
    # Location answers scored against NCBI's gene model instead (the 6 location
    # questions NCBI has the gene for), and right under either model.
    location_right_ncbi = sum(x$verdict_ncbi %in% "right"),
    location_right_either = sum(
      x$category == "location" & (x$verdict == "right" | x$verdict_ncbi %in% "right")
    ),
    # Repeats of one question are not independent: also count questions.
    questions_right_every_run = sum(tapply(x$verdict == "right", x$qid, all))
  )
}
per_arm <- do.call(rbind, lapply(names(arm_labels), function(a) {
  x <- r[r$arm == a, ]
  cbind(
    level = "arm", arm = a, category = "all", tally(x),
    cost_usd_mean = round(mean(x$cost_usd), 4),
    cost_usd_median = round(median(x$cost_usd), 4),
    cost_usd_total = round(sum(x$cost_usd), 2),
    seconds_mean = round(mean(x$seconds), 1),
    seconds_median = round(median(x$seconds), 1),
    seconds_max = max(x$seconds),
    tool_calls_mean = round(mean(lengths(x$tool_calls)), 2)
  )
}))
per_cat <- do.call(rbind, lapply(names(arm_labels), function(a) {
  do.call(rbind, lapply(names(cat_labels), function(k) {
    cbind(
      level = "arm_x_category", arm = a, category = k,
      tally(r[r$arm == a & r$category == k, ]),
      cost_usd_mean = NA, cost_usd_median = NA, cost_usd_total = NA,
      seconds_mean = NA, seconds_median = NA, seconds_max = NA, tool_calls_mean = NA
    )
  }))
}))
# Negative controls: answers needing no lookup, scored by the same scorer.
per_baseline <- do.call(rbind, lapply(names(baseline_labels), function(a) {
  x <- b[b$arm == a, ]
  cbind(
    level = "baseline", arm = a, category = if (all(x$category == "location")) "location" else "all",
    tally(x),
    cost_usd_mean = NA, cost_usd_median = NA, cost_usd_total = NA,
    seconds_mean = NA, seconds_median = NA, seconds_max = NA, tool_calls_mean = NA
  )
}))
# na = "NA", not "": the per-category rows end in empty cost/time fields, and
# a line ending in tabs is trailing whitespace the pre-commit hook strips,
# which would drop those columns.
write.table(rbind(per_arm, per_cat, per_baseline), file.path(outdir, "summary.tsv"),
  sep = "\t", quote = FALSE, row.names = FALSE, na = "NA"
)

# Statistics. The unit is the question: its 3 runs are not independent, so
# intervals resample questions (with all their runs), and paired tests
# compare per-question outcomes.
set.seed(20261016)
n_boot <- 2000
qids <- sort(unique(r$qid))
boots <- replicate(n_boot, sample(qids, length(qids), replace = TRUE), simplify = FALSE)
values <- data.frame(
  accuracy = as.numeric(r$verdict == "right"), cost_usd_median = r$cost_usd,
  seconds_median = r$seconds, tool_calls_mean = lengths(r$tool_calls)
)
metrics <- list(accuracy = mean, cost_usd_median = median, seconds_median = median, tool_calls_mean = mean)
# Row numbers of each setup's runs, per question.
rows_by_q <- lapply(setNames(names(arm_labels), names(arm_labels)), function(a) {
  split(which(r$arm == a), r$qid[r$arm == a])
})
interval <- function(v) unname(quantile(v, c(0.025, 0.975)))
boot_rows <- list()
for (m in names(metrics)) {
  g <- metrics[[m]]
  v <- values[[m]]
  f <- function(a, ids = qids) g(v[unlist(rows_by_q[[a]][ids], use.names = FALSE)])
  for (a in names(arm_labels)) {
    est <- f(a)
    ci <- interval(vapply(boots, function(ids) f(a, ids), 0))
    boot_rows[[length(boot_rows) + 1]] <- data.frame(
      test = "bootstrap_95ci", metric = m, arm = a, versus = NA,
      estimate = est, lo = ci[1], hi = ci[2]
    )
  }
  # The difference, resampling the same questions for both setups.
  for (pair in list(c("mcp", "web"), c("mcp", "bare"))) {
    est <- f(pair[1]) - f(pair[2])
    ci <- interval(vapply(boots, function(ids) f(pair[1], ids) - f(pair[2], ids), 0))
    boot_rows[[length(boot_rows) + 1]] <- data.frame(
      test = "bootstrap_95ci_difference", metric = m, arm = pair[1], versus = pair[2],
      estimate = est, lo = ci[1], hi = ci[2]
    )
  }
}
boot_tab <- do.call(rbind, boot_rows)
# Paired exact McNemar (a sign test on the discordant questions): is the
# question right in every run, for setup a against setup b? "valid only"
# drops invalid runs first, and questions left with none.
right_every <- function(x) tapply(x$verdict == "right", x$qid, all)
mcnemar <- function(a, bb, only_valid) {
  pick <- function(arm) {
    x <- r[r$arm == arm & (!only_valid | r$valid), ]
    right_every(x)
  }
  ya <- pick(a)
  yb <- pick(bb)
  common <- intersect(names(ya), names(yb))
  ya <- ya[common]
  yb <- yb[common]
  n10 <- sum(ya & !yb)
  n01 <- sum(!ya & yb)
  p <- if (n10 + n01 > 0) binom.test(n10, n10 + n01)$p.value else 1
  data.frame(
    test = if (only_valid) "mcnemar_exact_valid_runs" else "mcnemar_exact",
    metric = "questions_right_every_run", arm = a, versus = bb,
    estimate = sum(ya) - sum(yb), lo = NA, hi = NA,
    questions = length(common), a_only = n10, versus_only = n01,
    both = sum(ya & yb), neither = sum(!ya & !yb), p_value = signif(p, 3)
  )
}
paired <- do.call(rbind, lapply(c(FALSE, TRUE), function(v) {
  rbind(mcnemar("mcp", "web", v), mcnemar("mcp", "bare", v), mcnemar("web", "bare", v))
}))
boot_tab$questions <- length(qids)
boot_tab[c("a_only", "versus_only", "both", "neither", "p_value")] <- NA
num <- c("estimate", "lo", "hi")
stats_tab <- rbind(boot_tab, paired)
stats_tab[num] <- lapply(stats_tab[num], function(v) signif(v, 4))
write.table(stats_tab, file.path(outdir, "stats.tsv"),
  sep = "\t", quote = FALSE, row.names = FALSE, na = "NA"
)
print(stats_tab)

setup_note <- sprintf("Claude Opus 5.5 in Claude Code, %d questions × %d runs.", n_q, n_rep)
invalid_note <- sprintf(
  "Counted here, though they made no tool call: %d web-search and %d plant-genomics-mcp runs (answered from memory).",
  sum(!r$valid & r$arm == "web"), sum(!r$valid & r$arm == "mcp")
)
loc_either <- function(a) {
  x <- r[r$arm == a & r$category == "location", ]
  sprintf("%d of %d", sum(x$verdict == "right" | x$verdict_ncbi %in% "right"), nrow(x))
}
key_note <- paste0(
  "Answers checked against Ensembl Plants and UniProt, the databases the server reads;\n",
  "an answer from another annotation counts as wrong. Accepting NCBI's gene model too, location answers right: ",
  "web search ", loc_either("web"), ", server ", loc_either("mcp"), "."
)
# Count labels: white on green and red, dark on the light grey of declined.
verdict_text <- c(`TRUE` = "grey15", `FALSE` = "white")
legend_counts <- function(v) {
  sprintf("%s (%s)", v, vapply(v, function(k) {
    paste(vapply(names(arm_labels), function(a) {
      as.character(sum(r$verdict == k & r$arm == a))
    }, ""), collapse = " / ")
  }, ""))
}

# Figure 1: one bar per setup, segments = verdict share, labelled with counts;
# above them, the two no-model baselines (negative controls).
fig1 <- rbind(
  data.frame(bar = sprintf("%s (%d)", arm_labels[r$arm], n_ans), verdict = r$verdict),
  data.frame(
    bar = sprintf("%s (%d)", baseline_labels[b$arm], as.numeric(table(b$arm)[b$arm])),
    verdict = b$verdict
  )
)
bar_levels <- c(
  sprintf("%s (%d)", rev(arm_labels), n_ans),
  sprintf("%s (%d)", rev(baseline_labels), as.numeric(table(b$arm)[rev(names(baseline_labels))]))
)
fig1$arm_label <- factor(fig1$bar, levels = bar_levels)
fig1$verdict <- factor(fig1$verdict, levels = names(pal_verdict))
counts <- as.data.frame(table(arm_label = fig1$arm_label, verdict = fig1$verdict))
counts <- counts[counts$Freq > 0, ]
counts$share <- counts$Freq / ave(counts$Freq, counts$arm_label, FUN = sum)
p1 <- ggplot(counts, aes(x = share, y = arm_label, fill = verdict)) +
  geom_col(width = 0.62, position = position_stack(reverse = TRUE)) +
  geom_text(
    aes(label = ifelse(share >= 0.05, Freq, ""), colour = verdict == "declined"),
    position = position_stack(vjust = 0.5, reverse = TRUE),
    fontface = "bold", size = 4.2, show.legend = FALSE
  ) +
  scale_colour_manual(values = verdict_text, guide = "none") +
  # Slices too thin to hold a number are counted in the legend instead, per
  # setup in the order the title names.
  scale_fill_manual(
    values = pal_verdict, labels = legend_counts, drop = TRUE,
    name = "Answers (alone / web search / plant-genomics-mcp):"
  ) +
  guides(fill = guide_legend(title.position = "top", title.hjust = 0.5)) +
  scale_x_continuous(labels = scales::percent, expand = c(0, 0)) +
  labs(
    x = "Share of answers (count in brackets)", y = NULL,
    title = "Plant gene lookups: right, wrong or declined",
    subtitle = paste0(setup_note, "\n", key_note, "\n", invalid_note)
  ) +
  theme_pgmcp(base_size = 13) +
  theme(panel.grid.major.y = element_blank(), plot.margin = margin(8, 24, 8, 8))
ggsave(file.path(outdir, "agent_qa.png"), p1, width = 11.5, height = 5.4, dpi = 200, bg = "white")

# Figure 2: cost and seconds per answer, one point per run, box over them.
long <- rbind(
  data.frame(arm_label = r$arm_label, metric = "Cost per answer (US$, log scale)", value = r$cost_usd),
  data.frame(arm_label = r$arm_label, metric = "Seconds per answer (log scale)", value = r$seconds)
)
p2 <- ggplot(long, aes(x = value, y = arm_label)) +
  geom_boxplot(width = 0.55, outlier.shape = NA, fill = "white", colour = "grey20") +
  geom_point(
    position = position_jitter(height = 0.18, seed = 1),
    shape = 21, size = pgmcp_point_size - 0.6, stroke = pgmcp_point_stroke,
    fill = pgmcp_bench_point_fill, colour = pgmcp_point_outline, alpha = 0.55
  ) +
  facet_wrap(~metric, ncol = 2, scales = "free_x") +
  scale_x_log10(labels = scales::label_number(drop0trailing = TRUE)) +
  labs(
    x = NULL, y = NULL,
    title = "Cost and time per answer",
    subtitle = paste0(
      setup_note, "\nOne point per run; the box spans the middle half, the line in it is the median."
    )
  ) +
  theme_pgmcp(base_size = 13) +
  theme(
    panel.grid.major.y = element_blank(), strip.text = element_text(hjust = 0, face = "bold"),
    plot.margin = margin(8, 24, 8, 8)
  )
ggsave(file.path(outdir, "agent_qa_cost_time.png"), p2, width = 12, height = 4.2, dpi = 200, bg = "white")

# Figure 3: verdicts per setup x kind of question; strips name the counts.
n_cat <- table(r$category[r$arm == names(arm_labels)[1]])
strip <- setNames(
  sprintf("%s (%d questions × %d runs)", cat_labels, n_cat[names(cat_labels)] / n_rep, n_rep),
  names(cat_labels)
)
r$cat_strip <- factor(strip[r$category], levels = strip)
cc <- as.data.frame(table(arm_label = r$arm_label, cat_strip = r$cat_strip, verdict = r$verdict))
cc <- cc[cc$Freq > 0, ]
p3 <- ggplot(cc, aes(x = Freq, y = arm_label, fill = verdict)) +
  geom_col(width = 0.62, position = position_stack(reverse = TRUE)) +
  geom_text(
    aes(label = Freq, colour = verdict == "declined"),
    position = position_stack(vjust = 0.5, reverse = TRUE),
    fontface = "bold", size = 3.4, show.legend = FALSE
  ) +
  scale_colour_manual(values = verdict_text, guide = "none") +
  facet_wrap(~cat_strip, ncol = 1, scales = "free_x") +
  scale_fill_manual(values = pal_verdict, name = NULL, drop = TRUE) +
  scale_x_continuous(expand = c(0, 0)) +
  labs(
    x = "Answers", y = NULL, title = "Right, wrong or declined, by kind of question",
    subtitle = setup_note
  ) +
  theme_pgmcp(base_size = 12) +
  theme(
    panel.grid.major.y = element_blank(), strip.text = element_text(hjust = 0, face = "bold"),
    plot.margin = margin(8, 24, 8, 8)
  )
ggsave(file.path(outdir, "agent_qa_by_category.png"), p3, width = 9, height = 8.5, dpi = 200, bg = "white")

print(per_arm)
