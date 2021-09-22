#!/usr/bin/env python3

"""
    A script to demonstrate the Conduit EcoacousticsDataModule, using it to generate some
    representations of the audio samples and clasisfying them with a random forest.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from conduit.data.datamodules.audio import EcoacousticsDataModule
from sklearn import ensemble, metrics, model_selection
from tqdm import tqdm


def gen_reps(datamodule, out_dir, save=True):
    """Pass data through VGGish to obtain 128dim represntations of samples."""

    # Get data loader.
    data_loader = datamodule.train_dataloader()

    # Define device.
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # Load pre-trained VGGish.
    model = torch.hub.load(
        "DavidHurst/torchvggish",
        "vggish",
        preprocess=False,
        device=device,
        postprocess=False,
    )

    # Pass all samples through VGGish, storing output representation and correspondig label.
    model.eval()
    reps_ls = []
    labels_ls = []
    with torch.no_grad():
        for data in tqdm(data_loader, desc="Generating Representations"):
            # Crop spectrogram to meet VGGish's expected dimensions.
            specgram = data.x[:, :, :, :96]

            # Transpose spectrogram to get time on the vertical axis instead of freq bins.
            specgram = torch.transpose(specgram, -2, -1)

            # VGGish expects log-transformed spectrograms.
            specgram = torch.log(specgram + 10e-4).to(device)
            y = data.y.to(device)

            rep = model(specgram)
            reps_ls.append(rep.cpu().numpy())
            labels_ls.append(y.cpu().numpy())

    reps = np.concatenate(reps_ls)
    labels = np.concatenate(labels_ls)

    if save:
        np.savez(out_dir / "representations.npz", representation=reps, label=labels)

    # Save memory, delete spectrogram encoder as it's no longer needed.
    del model

    return reps, labels


def run_random_forest(representations, labels, n_trials=3):
    """Apply a random forest classifier to the representations generated by VGGish"""

    avg_f1 = 0.0
    # Average F1 score over n_trails runs.
    for i in tqdm(range(n_trials), desc="Random Forest Run"):
        # Split data into train and test for random forest.
        X_train, X_test, y_train, y_test = model_selection.train_test_split(
            representations, labels, test_size=0.20
        )

        # Fit random forest to data and get predictions.
        rf_clf = ensemble.RandomForestClassifier()
        rf_clf.fit(X_train, y_train)
        y_pred = rf_clf.predict(X_test)

        # Evaluate classifier's performance.
        f1 = metrics.f1_score(y_test, y_pred, average="micro")
        avg_f1 += f1 / n_trials

    # Generate confusion matrix for last run.
    cm = metrics.confusion_matrix(y_test, y_pred, normalize="all")

    return avg_f1, cm


def main():
    root_dir = Path().resolve().parent.parent

    # Initialise Conduit datamodule (stores dataloaders).
    dm = EcoacousticsDataModule(
        root_dir,
        specgram_segment_len=0.96,
        test_prop=0,
        val_prop=0,
        train_batch_size=1,
        num_freq_bins=64,
    )
    dm.prepare_data()
    dm.setup()

    # Pass all data through VGGish.
    representations, labels = gen_reps(dm, root_dir)

    # Classify representations using a random forest.
    avg_f1, cm = run_random_forest(representations, labels)
    print(f"Random forest average F1 score: {avg_f1 * 100:.2f}%.")
    print(f"Confusion Matrix: \n {cm}")

    fig, ax = plt.subplots()
    metrics.ConfusionMatrixDisplay(cm).plot(ax=ax)
    fig.savefig(root_dir / "random_forest_cm.png")


if __name__ == "__main__":
    main()
