# SharePoint Generative AI Agent

This project is a Python script that connects to a Microsoft SharePoint site, retrieves documents from a specified folder, and uses OpenAI's GPT-4 model to analyze the documents and generate summaries or insights.

## Prerequisites
1. Python 3.8 or higher.
2. A Microsoft SharePoint site with documents stored in a folder.
3. An OpenAI API key.

## Inputs
SharePoint Credentials: Username and password for SharePoint authentication.

SharePoint Site URL: The URL of the SharePoint site.

SharePoint Folder Path: The folder path within the SharePoint site where documents are stored.

OpenAI API Key: API key for accessing OpenAI's GPT model.

Documents: Text-based documents (e.g., .txt, .docx, .pdf) stored in the specified SharePoint folder.

## Outputs
Document Analysis: A summary and key insights for each document analyzed by the AI model.

Console Output: The script prints the analysis results to the console.

## How to Use
1. Replace the placeholders in the script with your SharePoint and OpenAI credentials.
2. Store your credentials in environment variables for security.
3. Run the script to analyze documents in your SharePoint folder.

## Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/sharepoint-ai-agent.git
   cd sharepoint-ai-agent
