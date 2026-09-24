"""Construct the public LangGraph workflow."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.nodes import cad, confirmation, fluent, results, review
from src.services.terminal import progress_node
from src.state import PipelineState


def build_graph(checkpoint_path: str | Path):
    """Build a graph with a durable SQLite checkpoint database."""
    builder = StateGraph(PipelineState)

    def add_node(name, operation):
        builder.add_node(name, progress_node(name, operation))

    add_node("prepare", cad.prepare)
    add_node("query_geometry", cad.query_geometry)
    add_node("understand_prompt", cad.understand_prompt)
    add_node("verify_selection", cad.verify_selection)
    add_node("extract_volume", cad.extract_volume)
    add_node("label_faces", cad.label_faces)
    add_node("validate_cad", cad.validate_cad)
    add_node("human_confirmation", confirmation.human_confirmation)
    add_node("human_intervention", confirmation.human_intervention)
    add_node("reload_confirmed_cad", confirmation.reload_confirmed_cad)
    add_node("launch_fluent", fluent.launch_fluent)
    add_node("rebuild_fluent", fluent.rebuild_fluent)
    for step in fluent.FLUENT_STEPS:
        add_node(step, fluent.fluent_step(step))
    add_node("final_validation", fluent.validate_mesh)
    add_node("review_failure", review.review_failure)
    add_node("apply_repair", review.apply_repair)
    add_node("completed", results.completed)
    add_node("failed", results.failed)
    add_node("cancelled", confirmation.cancelled)

    normal = [
        "prepare",
        "query_geometry",
        "understand_prompt",
        "verify_selection",
        "extract_volume",
        "label_faces",
        "validate_cad",
    ]
    builder.add_edge(START, normal[0])
    for current, following in zip(normal, normal[1:]):
        builder.add_conditional_edges(
            current,
            results.has_error,
            {
                "review_failure": "review_failure",
                "continue": following,
            },
        )
    builder.add_conditional_edges(
        "validate_cad",
        results.has_error,
        {
            "review_failure": "review_failure",
            "continue": "human_confirmation",
        },
    )
    builder.add_conditional_edges(
        "human_confirmation",
        confirmation.confirmation_route,
        {
            "cancelled": "cancelled",
            "reload_confirmed_cad": "reload_confirmed_cad",
        },
    )
    builder.add_conditional_edges(
        "reload_confirmed_cad",
        results.has_error,
        {
            "review_failure": "review_failure",
            "continue": "launch_fluent",
        },
    )
    fluent_nodes = ["launch_fluent", *fluent.FLUENT_STEPS, "final_validation"]
    for current, following in zip(fluent_nodes, fluent_nodes[1:]):
        builder.add_conditional_edges(
            current,
            results.has_error,
            {
                "review_failure": "review_failure",
                "continue": following,
            },
        )
    builder.add_conditional_edges(
        "final_validation",
        results.has_error,
        {
            "review_failure": "review_failure",
            "continue": "completed",
        },
    )
    builder.add_conditional_edges(
        "review_failure",
        results.review_route,
        {
            "failed": "failed",
            "apply_repair": "apply_repair",
        },
    )
    builder.add_edge("completed", END)
    builder.add_edge("failed", END)
    builder.add_edge("cancelled", END)

    connection = sqlite3.connect(Path(checkpoint_path), check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    return builder.compile(checkpointer=checkpointer)
