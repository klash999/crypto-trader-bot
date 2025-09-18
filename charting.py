import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import pandas as pd

def plot_hourly_with_targets(df_hour: pd.DataFrame, targets: list, stop: float, title: str) -> bytes:
    fig, ax = plt.subplots(figsize=(10,5))
    df = df_hour.copy()
    if not df.empty:
        ax.plot(df.index, df['Close'], linewidth=1.5)
    ax.set_title(title)
    ax.set_xlabel('Time')
    ax.set_ylabel('Price')
    if not df.empty:
        for i, tg in enumerate(targets, start=1):
            ax.axhline(tg, linewidth=1.1, linestyle='--')
            ax.text(df.index[-1], tg, f'T{i}', fontsize=8)
        ax.axhline(stop, linewidth=1.1)
        ax.text(df.index[-1], stop, 'SL', fontsize=8)
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
