# -*- coding: utf-8 -*-
"""Triplet lossOpenAI_CLIP_simple_implementation_Rico.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1vH9yq-cwsdBRJHjt7cE8pUxDydzIE_es

## Introduction

Given a query (raw text) like "pop-up message showing about free access	" or "a description page of shopping app", the model will retrieve the most relevant images:
"""

!pip install timm
!pip install transformers

import os
import cv2
import gc
import numpy as np
import pandas as pd
import itertools
from tqdm.autonotebook import tqdm
import albumentations as A
import matplotlib.pyplot as plt

import torch
from torch import nn
import torch.nn.functional as F
import timm
from transformers import DistilBertModel, DistilBertConfig, DistilBertTokenizer

from google.colab import drive
drive.mount("/content/drive", force_remount=True)

!unzip /content/drive/MyDrive/trainImages.zip

!unzip /content/drive/MyDrive/testImages.zip -d /content/

!unzip /content/drive/MyDrive/validImages.zip -d /content/

folder_path = "/content/trainImages"

# Get the list of files in the folder
file_list = os.listdir(folder_path)

# Count the number of files
num_files = len(file_list)

print(f"Number of files in the folder: {num_files}")

"""# Show one image from dataset"""

from google.colab.patches import cv2_imshow

image = cv2.imread("/content/trainImages/57715.jpg")

image = cv2.imread("/content/testImages/55300.jpg")

cv2_imshow(image)

"""## Some pre-preocessing"""

# for train and validation
import pandas as pd

df = pd.read_csv("captions.csv")
image_path = "/content/trainImages"
captions_path = "/content"

# for test
import pandas as pd

df = pd.read_csv("captions.csv")
image_path = "/content/testImages"
captions_path = "/content"

# Debugging: Print image paths
for index, row in df.iterrows():
        image_filename = row['image']
        image_full_path = os.path.join(image_path, image_filename)
        print(f"Processing image: {image_full_path}")

df.head(10)

df.shape

"""## Config"""

class CFG:
    margin = 1.0
    debug = False
    image_path = image_path
    captions_path = captions_path
    batch_size = 32
    num_workers = 2
    head_lr = 1e-3
    image_encoder_lr = 1e-4
    text_encoder_lr = 1e-5
    weight_decay = 1e-3
    patience = 1
    factor = 0.8
    epochs = 4
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model_name = 'resnet50'
    image_embedding = 2048
    text_encoder_model = "distilbert-base-uncased"
    text_embedding = 768
    text_tokenizer = "distilbert-base-uncased"
    max_length = 200

    pretrained = True # for both image encoder and text encoder
    trainable = True # for both image encoder and text encoder
    temperature = 1.0

    # image size
    size = 224

    # for projection head; used for both image and text encoders
    num_projection_layers = 1
    projection_dim = 256
    dropout = 0.1

"""## Utils"""

class AvgMeter:
    def __init__(self, name="Metric"):
        self.name = name
        self.reset()

    def reset(self):
        self.avg, self.sum, self.count = [0] * 3

    def update(self, val, count=1):
        self.count += count
        self.sum += val * count
        self.avg = self.sum / self.count

    def __repr__(self):
        text = f"{self.name}: {self.avg:.4f}"
        return text

def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group["lr"]

"""## Dataset"""

class CLIPDataset(torch.utils.data.Dataset):
    def __init__(self, image_filenames, captions,negimage_filenames, tokenizer, transforms):
        """
        image_filenames and cpations must have the same length; so, if there are
        multiple captions for each image, the image_filenames must have repetitive
        file names
        """

        self.image_filenames = image_filenames
        self.negimage_filenames = negimage_filenames
        self.captions = list(captions)
        self.encoded_captions = tokenizer(
            list(captions), padding=True, truncation=True, max_length=CFG.max_length
        )
        self.transforms = transforms

    def __getitem__(self, idx):
        item = {
            key: torch.tensor(values[idx])
            for key, values in self.encoded_captions.items()
        }

        image = cv2.imread(f"{CFG.image_path}/{self.image_filenames[idx]}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = self.transforms(image=image)['image']
        item['image'] = torch.tensor(image).permute(2, 0, 1).float()
        negimage = cv2.imread(f"{CFG.image_path}/{self.negimage_filenames[idx]}")
        negimage = cv2.cvtColor(negimage, cv2.COLOR_BGR2RGB)
        negimage = self.transforms(image=negimage)['image']
        item['negimage'] = torch.tensor(negimage).permute(2, 0, 1).float()
        item['caption'] = self.captions[idx]

        return item


    def __len__(self):
        return len(self.captions)



def get_transforms(mode="train"):
    if mode == "train":
        return A.Compose(
            [
                A.Resize(CFG.size, CFG.size, always_apply=True),
                A.Normalize(max_pixel_value=255.0, always_apply=True),
            ]
        )
    else:
        return A.Compose(
            [
                A.Resize(CFG.size, CFG.size, always_apply=True),
                A.Normalize(max_pixel_value=255.0, always_apply=True),
            ]
        )

"""## Image Encoder"""

class ImageEncoder(nn.Module):
    """
    Encode images to a fixed size vector
    """

    def __init__(
        self, model_name=CFG.model_name, pretrained=CFG.pretrained, trainable=CFG.trainable
    ):
        super().__init__()
        self.model = timm.create_model(
            model_name, pretrained, num_classes=0, global_pool="avg"
        )
        for p in self.model.parameters():
            p.requires_grad = trainable

    def forward(self, x):
        return self.model(x)

"""## Text Encoder"""

class TextEncoder(nn.Module):
    def __init__(self, model_name=CFG.text_encoder_model, pretrained=CFG.pretrained, trainable=CFG.trainable):
        super().__init__()
        if pretrained:
            self.model = DistilBertModel.from_pretrained(model_name)
        else:
            self.model = DistilBertModel(config=DistilBertConfig())

        for p in self.model.parameters():
            p.requires_grad = trainable

        # we are using the CLS token hidden representation as the sentence's embedding
        self.target_token_idx = 0

    def forward(self, input_ids, attention_mask):
        output = self.model(input_ids=input_ids, attention_mask=attention_mask)
        last_hidden_state = output.last_hidden_state
        return last_hidden_state[:, self.target_token_idx, :]

"""## Projection Head"""

class ProjectionHead(nn.Module):
    def __init__(
        self,
        embedding_dim,
        projection_dim=CFG.projection_dim,
        dropout=CFG.dropout
    ):
        super().__init__()
        self.projection = nn.Linear(embedding_dim, projection_dim)
        self.gelu = nn.GELU()
        self.fc = nn.Linear(projection_dim, projection_dim)
        self.dropout = nn.Dropout(dropout)
        self.layer_norm = nn.LayerNorm(projection_dim)

    def forward(self, x):
        projected = self.projection(x)
        x = self.gelu(projected)
        x = self.fc(x)
        x = self.dropout(x)
        x = x + projected
        x = self.layer_norm(x)
        return x

"""## CLIP

#### Check the cell below this code block for the continue of the explanations
"""

class TripletLoss(nn.Module):
    def __init__(self, margin=1.0):
        super(TripletLoss, self).__init__()
        self.margin = margin

    def calc_euclidean(self, x1, x2):
        return (x1 - x2).pow(2).sum(1)

    def forward(self, anchor: torch.Tensor, positive: torch.Tensor, negative: torch.Tensor) -> torch.Tensor:
        distance_positive = self.calc_euclidean(anchor, positive)
        distance_negative = self.calc_euclidean(anchor, negative)
        losses = torch.relu(distance_positive - distance_negative + self.margin)
        return losses.mean()

class CLIPModel(nn.Module):
    def __init__(
        self,
        temperature=CFG.temperature,
        image_embedding=CFG.image_embedding,
        text_embedding=CFG.text_embedding,
    ):
        super().__init__()
        self.image_encoder = ImageEncoder()
        self.text_encoder = TextEncoder()
        self.image_projection = ProjectionHead(embedding_dim=image_embedding)
        self.text_projection = ProjectionHead(embedding_dim=text_embedding)
        self.temperature = temperature

    def forward(self, batch):
        # Getting Image and Text Features
        image_features = self.image_encoder(batch["image"])
        negimage_features = self.image_encoder(batch["negimage"])
        text_features = self.text_encoder(
            input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]
        )
        # Getting Image and Text Embeddings (with same dimension)
        image_embeddings = self.image_projection(image_features)
        negimage_embeddings = self.image_projection(negimage_features)
        text_embeddings = self.text_projection(text_features)

        # Calculate the Triplet Loss
        loss_fn = TripletLoss(margin=CFG.margin)
        loss = loss_fn(text_embeddings, image_embeddings, negimage_embeddings)  # Pass embeddings as arguments

        return loss

"""## Train"""

def make_train_valid_dfs():
    dataframe = pd.read_csv(f"{CFG.captions_path}/captions.csv")
    max_id = dataframe["id"].max() + 1 if not CFG.debug else 100
    image_ids = np.arange(0, max_id)
    np.random.seed(42)
    valid_ids = np.random.choice(
        image_ids, size=int(0.2 * len(image_ids)), replace=False
    )
    train_ids = [id_ for id_ in image_ids if id_ not in valid_ids]
    train_dataframe = dataframe[dataframe["id"].isin(train_ids)].reset_index(drop=True)
    valid_dataframe = dataframe[dataframe["id"].isin(valid_ids)].reset_index(drop=True)
    return train_dataframe, valid_dataframe


def build_loaders(dataframe, tokenizer, mode):
    transforms = get_transforms(mode=mode)
    dataset = CLIPDataset(
        dataframe["image"].values,
        dataframe["caption"].values,
        dataframe["negimage"].values,
        tokenizer=tokenizer,
        transforms=transforms,
    )
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=CFG.batch_size,
        num_workers=CFG.num_workers,
        shuffle=True if mode == "train" else False,
    )
    return dataloader

def train_epoch(model, train_loader, optimizer, lr_scheduler, step):
    loss_meter = AvgMeter()
    tqdm_object = tqdm(train_loader, total=len(train_loader))
    for batch in tqdm_object:
        batch = {k: v.to(CFG.device) for k, v in batch.items() if k != "caption"}
        loss = model(batch)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if step == "batch":
            lr_scheduler.step()

        count = batch["image"].size(0)
        loss_meter.update(loss.item(), count)

        tqdm_object.set_postfix(train_loss=loss_meter.avg, lr=get_lr(optimizer))
    return loss_meter


def valid_epoch(model, valid_loader):
    loss_meter = AvgMeter()

    tqdm_object = tqdm(valid_loader, total=len(valid_loader))
    for batch in tqdm_object:
        batch = {k: v.to(CFG.device) for k, v in batch.items() if k != "caption"}
        loss = model(batch)

        count = batch["image"].size(0)
        loss_meter.update(loss.item(), count)

        tqdm_object.set_postfix(valid_loss=loss_meter.avg)
    return loss_meter


def main():
    train_df, valid_df = make_train_valid_dfs()
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)
    train_loader = build_loaders(train_df, tokenizer, mode="train")
    valid_loader = build_loaders(valid_df, tokenizer, mode="valid")


    model = CLIPModel().to(CFG.device)
    params = [
        {"params": model.image_encoder.parameters(), "lr": CFG.image_encoder_lr},
        {"params": model.text_encoder.parameters(), "lr": CFG.text_encoder_lr},
        {"params": itertools.chain(
            model.image_projection.parameters(), model.text_projection.parameters()
        ), "lr": CFG.head_lr, "weight_decay": CFG.weight_decay}
    ]
    optimizer = torch.optim.AdamW(params, weight_decay=0.)
    lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", patience=CFG.patience, factor=CFG.factor
    )
    step = "epoch"

    best_loss = float('inf')
    for epoch in range(CFG.epochs):
        print(f"Epoch: {epoch + 1}")
        model.train()
        train_loss = train_epoch(model, train_loader, optimizer, lr_scheduler, step)
        model.eval()
        with torch.no_grad():
            valid_loss = valid_epoch(model, valid_loader)

        if valid_loss.avg < best_loss:
            best_loss = valid_loss.avg
            torch.save(model.state_dict(), "best.pt")
            print("Saved Best Model!")

        lr_scheduler.step(valid_loss.avg)

main()

"""## Inference

### Getting Image Embeddings
"""

def get_image_embeddings(valid_df, model_path):
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)
    valid_loader = build_loaders(valid_df, tokenizer, mode="valid")

    model = CLIPModel().to(CFG.device)
    model.load_state_dict(torch.load(model_path, map_location=CFG.device))
    model.eval()

    valid_image_embeddings = []
    with torch.no_grad():
        for batch in tqdm(valid_loader):
            image_features = model.image_encoder(batch["image"].to(CFG.device))
            image_embeddings = model.image_projection(image_features)
            valid_image_embeddings.append(image_embeddings)
    return model, torch.cat(valid_image_embeddings)

_, valid_df = make_train_valid_dfs()
model, image_embeddings = get_image_embeddings(valid_df, "best.pt")

valid_df.head()

# save mode
torch.save(model.state_dict(), '/content/drive/My Drive/Finalsimplemodel.pth')

"""### Finding Matches

This function does the final task that we wished our model would be capable of: it gets the model, image_embeddings, and a text query. It will display the most relevant images from the validation set! Isn't it amazing? Let's see how it performs after all!
"""

def find_matches(model, image_embeddings, query, image_filenames, n=9):
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)
    encoded_query = tokenizer([query])
    batch = {
        key: torch.tensor(values).to(CFG.device)
        for key, values in encoded_query.items()
    }
    with torch.no_grad():
        text_features = model.text_encoder(
            input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]
        )
        text_embeddings = model.text_projection(text_features)

    image_embeddings_n = F.normalize(image_embeddings, p=2, dim=-1)
    text_embeddings_n = F.normalize(text_embeddings, p=2, dim=-1)
    dot_similarity = text_embeddings_n @ image_embeddings_n.T

    values, indices = torch.topk(dot_similarity.squeeze(0), n * 5)
    matches = [image_filenames[idx] for idx in indices[::5]]

    _, axes = plt.subplots(3, 3, figsize=(10, 10))
    for match, ax in zip(matches, axes.flatten()):
        image = cv2.imread(f"{CFG.image_path}/{match}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        ax.imshow(image)
        ax.axis("off")

    plt.show()

import shutil

# Unzip the testImages.zip file into a temporary folder
temp_folder = '/content/temp_extracted'
!unzip -q /content/drive/MyDrive/testImages.zip -d {temp_folder}

# Move the contents of the temporary folder to /content/trainImages
destination_folder = '/content/trainImages'
for item in os.listdir(temp_folder):
    source = os.path.join(temp_folder, item)
    destination = os.path.join(destination_folder, item)
    shutil.move(source, destination)

# Clean up the temporary folder
shutil.rmtree(temp_folder)

print("Contents moved to /content/trainImages.")

!unzip /content/drive/MyDrive/testImages.zip -d /content/trainImages

# test
model_path="/content/drive/My Drive/Finalsimplemodel.pth"
model = CLIPModel().to(CFG.device)
model.load_state_dict(torch.load(model_path, map_location=CFG.device))
model.eval()
tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)

def get_image_embeddings_within_app(valid_df):
    valid_loader = build_loaders(valid_df, tokenizer, mode="valid")
    valid_image_embeddings = []
    with torch.no_grad():
        for batch in tqdm(valid_loader):
            image_features = model.image_encoder(batch["image"].to(CFG.device))
            #print(image_features)
            image_embeddings = model.image_projection(image_features)
            #print(image_embeddings)
            valid_image_embeddings.append(image_embeddings)
            #print(valid_image_embeddings)
    return torch.cat(valid_image_embeddings)

grouped_df = df.groupby('activity_name')

for activity_name, activity_group_df in grouped_df:
     image_ids = activity_group_df['image'].unique().tolist()
     image_ids = image_ids[:8]
     sampled_images_df = activity_group_df[activity_group_df['image'].isin(image_ids)]

query='add icon and download option in bible reading tracking app'

actual_image = activity_group_df[activity_group_df['caption'] == query]['image'].iloc[0]

image_filenames

import random

def find_matches_score_all(model, df, k=2,M=6):
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)

    #image_filenames = df['image'].values
    total_queries = len(df)  # Total number of queries

    cumulative_score = 0  # Initialize cumulative score
    ground_truth_list = []  # List to store ground truth labels
    prediction_list = []     # List to store model predictions
    grouped_df = df.groupby('activity_name')

    for activity_name, activity_group_df in grouped_df:
        print(f"Activity Name: {activity_name}")
        # Get unique image IDs for this activity group
        image_ids = activity_group_df['image'].unique().tolist()
        # If the activity group has fewer than M images, sample from other groups
        if len(image_ids) < M:
            other_groups = [other_group_df for other_activity, other_group_df in grouped_df if other_activity != activity_name]
            other_image_ids = [id for other_group_df in other_groups for id in other_group_df['image'].unique()]
            sampled_ids = random.sample(other_image_ids, min(M - len(image_ids), len(other_image_ids)))
            image_ids.extend(sampled_ids)

        # Convert the set of unique image IDs back to a list
        image_ids = image_ids[:M]
        sampled_images_df = df[df['image'].isin(image_ids)]
        image_filenames = sampled_images_df.values
        print(' image file names is :',image_filenames)
        image_embeddings = get_image_embeddings_within_app(sampled_images_df)

        # Loop through captions for this activity group
        captions = activity_group_df['caption'].tolist()
        for query in captions:
            # Filter the DataFrame to find the actual image based on the caption
            actual_image = activity_group_df[activity_group_df['caption'] == query]['image'].iloc[0]
            print(' actual image  is :',actual_image)
            print(' caption is :', query)
            print(' actual image is :', actual_image)
            print('test images are :',image_ids)
            encoded_query = tokenizer([query], padding=True, truncation=True, return_tensors="pt")
            batch = {key: tensor.to(CFG.device) for key, tensor in encoded_query.items()}

            with torch.no_grad():
                text_features = model.text_encoder(
                    input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]
                )
                text_embeddings = model.text_projection(text_features)

            image_embeddings_n = F.normalize(image_embeddings, p=2, dim=-1)
            text_embeddings_n = F.normalize(text_embeddings, p=2, dim=-1)
            dot_similarity = text_embeddings_n @ image_embeddings_n.T
            values, indices = torch.topk(dot_similarity.squeeze(0), k*5)
            print(' value k is :',values)
            matches = [image_filenames[idx] for idx in indices[::5]]
            top_matches = matches  # Get the top k matches
            predictions_k = matches[0]  # Predicted image names
            print(' prediction k is :',predictions_k)
            prediction_list.append(predictions_k[0])  # Store the predicted image filename
            #prediction_list.append(predictions_k)  # Store the model predictions

            score = 1 if actual_image in predictions_k else 0  # Calculate the score
            cumulative_score += score  # Update cumulative score

            ground_truth_list.append(actual_image)  # Store the ground truth label
            print(f"Caption: {query}, Actual Image: {actual_image}, Top Matches: {predictions_k},  Score: {score}")

    final_score = cumulative_score / len(df)  # Calculate final score
    print(f"Final Score: {cumulative_score}")
    print(f"Final Score: {final_score}")
    return final_score,ground_truth_list, prediction_list

# Commented out IPython magic to ensure Python compatibility.
# %%capture
# final_score,ground_truth, predictions = find_matches_score_all(model, df, k=2,M=6)

predictions

captions = df.groupby('activity_name')

android_captions = df[df['activity_name']=='android']
android_captions

captions = df.groupby('image')['caption'].first().reset_index()
image_filenames = selected_rows['image'].values
find_matches_with_score(model, image_embeddings, captions, image_filenames, n=3)

image_filenames=selected_rows['image'].values
image_filenames

find_matches(model,
             image_embeddings,
             query="pop up to choose mailing application",
             image_filenames=selected_rows['image'].values,
             n=3)

find_matches(model,
             image_embeddings,
             query="screen showing list of various countries for an app",
             image_filenames=valid_df['image'].values,
             n=9)

valid_df['image'].values

selected_rows = df[df["activity_name"] == "com.android.contacts"]

selected_rows.shape

selected_rows.head()

find_matches(model,
             image_embeddings,
             query="display of a footwear options in a shopping app",
             image_filenames=valid_df['image'].values,
             n=9)

find_matches(model,
             image_embeddings,
             query="list of diet plans showing in application",
             image_filenames=valid_df['image'].values,
             n=9)

find_matches(model,
             image_embeddings,
             query="pop up displaying introduction for the app",
             image_filenames=valid_df['image'].values,
             n=9)

image_folder_path = '/content/drive/MyDrive/test'
caption_file_path = '/content/captions.csv'

image = cv2.imread("/content/drive/MyDrive/test/31677.jpg")
cv2_imshow(image)

def get_image_embeddings(valid_df, model_path):
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)
    valid_loader = build_loaders(valid_df, tokenizer, mode="valid")
    model = CLIPModel().to(CFG.device)
    model.load_state_dict(torch.load(model_path, map_location=CFG.device))
    model.eval()

    valid_image_embeddings = []
    with torch.no_grad():
        for batch in tqdm(valid_loader):
            image_features = model.image_encoder(batch["image"].to(CFG.device))
            print(image_features)
            image_embeddings = model.image_projection(image_features)
            print(image_embeddings)
            valid_image_embeddings.append(image_embeddings)
            print(valid_image_embeddings)
    return model, torch.cat(valid_image_embeddings)

df.head(10)

df.shape

import pandas as pd
import matplotlib.pyplot as plt

app_screen_counts = df['activity_name'].value_counts()

app_screen_counts.shape
# number of unique apps

app_screen_counts.head(20)

app_screen_counts = pd.DataFrame(app_screen_counts)
new_columns = ['screen_shots_counts']

# Reset column names
app_screen_counts.columns = new_columns
app_screen_counts.reset_index(inplace=True)

# Rename the 'index' column to 'image count'
app_screen_counts.rename(columns={'index': 'app name'}, inplace=True)
app_screen_counts.head(20)

screencounts = df['activity_name'].value_counts()

unique_counts=screencounts.unique()
unique_counts

screen_app_count=app_screen_counts['screen_shots_counts'].value_counts()

screen_app_count = pd.DataFrame(screen_app_count)
new_columns = ['no. of apps has this no. of screen count']
screen_app_count.columns = new_columns

screen_app_count.reset_index(inplace=True)

# Rename the 'index' column to 'image count'
screen_app_count.rename(columns={'index': 'no. of screenshots'}, inplace=True)
screen_app_count.head(20)

screen_app_count.shape

screen_app_count.head(39)

import torch
from sklearn.metrics import precision_score, recall_score, f1_score, accuracy_score, top_k_accuracy_score

accuracy = accuracy_score(ground_truth, predictions)

accuracy

precision = precision_score(ground_truth, predictions, average='weighted')
precision

recall = recall_score(ground_truth, predictions, average='weighted')
f1 = f1_score(ground_truth, predictions, average='weighted')
recall

f1 = f1_score(ground_truth, predictions, average='weighted')
f1

# Commented out IPython magic to ensure Python compatibility.
# %%capture
# final_score,ground_truth, predictions = find_matches_score_all(model, df, k=4,M=20)

final_score

from sklearn.metrics import precision_score
import random

def calculate_custom_precision(ground_truth, predictions, k):
    custom_predictions = []

    for true_image, predicted_images in zip(ground_truth, predictions):
        if true_image in predicted_images:
            custom_predictions.append([true_image])  # Use the true image if present
        else:
            # Choose any other image from the prediction list if true image is not present
            random_prediction = random.choice(predicted_images)
            custom_predictions.append([random_prediction])

    return precision_score(ground_truth, custom_predictions, average='weighted')

# Assuming you have ground_truth (a list of image IDs) and predictions (a list of predicted image IDs, each sublist corresponds to a different k)
k = 4  # You can change this to your desired k value (k > 1)
precision_at_k = calculate_custom_precision(ground_truth, predictions, k)
print(f"Custom Precision@{k}: {precision_at_k}")

from sklearn.metrics import recall_score, f1_score
import random

def calculate_custom_recall(ground_truth, predictions, k):
    custom_predictions = []

    for true_image, predicted_images in zip(ground_truth, predictions):
        if true_image in predicted_images:
            custom_predictions.append([true_image])  # Use the true image if present
        else:
            # Choose any other image from the prediction list if true image is not present
            random_prediction = random.choice(predicted_images)
            custom_predictions.append([random_prediction])

    return recall_score(ground_truth, custom_predictions, average='weighted')

def calculate_custom_f1_score(ground_truth, predictions, k):
    custom_predictions = []

    for true_image, predicted_images in zip(ground_truth, predictions):
        if true_image in predicted_images:
            custom_predictions.append([true_image])  # Use the true image if present
        else:
            # Choose any other image from the prediction list if true image is not present
            random_prediction = random.choice(predicted_images)
            custom_predictions.append([random_prediction])

    return f1_score(ground_truth, custom_predictions, average='weighted')

# Calculate custom recall
recall_at_k = calculate_custom_recall(ground_truth, predictions, k)
print(f"Custom Recall@{k}: {recall_at_k}")

# Calculate custom F1 score
f1_score_at_k = calculate_custom_f1_score(ground_truth, predictions, k)
print(f"Custom F1 Score@{k}: {f1_score_at_k}")