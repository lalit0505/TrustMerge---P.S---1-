import typer

app = typer.Typer(
    help="TrustMerge: conflict provenance for Git.", no_args_is_help=True
)


@app.callback()
def main() -> None:
    """Show where each side of a conflict came from and whether it was tested."""


@app.command()
def version() -> None:
    """Print the version."""
    from trustmerge import __version__

    typer.echo(__version__)
