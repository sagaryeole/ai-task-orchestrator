import csv
import os

def csv_to_markdown(input_csv, output_folder="TestOutput", output_filename="name_of_subject.md"):
    """
    Reads a CSV file and writes its contents to a Markdown file.
    
    Args:
        input_csv (str): Path to the input CSV file.
        output_folder (str): Folder where the output Markdown file will be saved.
        output_filename (str): Name of the output Markdown file.
    """
    if not os.path.exists(input_csv):
        raise FileNotFoundError(f"The file {input_csv} does not exist.")

    os.makedirs(output_folder, exist_ok=True)
    output_path = os.path.join(output_folder, output_filename)

    with open(input_csv, 'r', newline='', encoding='utf-8') as csv_file:
        reader = csv.reader(csv_file)
        rows = list(reader)

        with open(output_path, 'w', encoding='utf-8') as md_file:
            # Write Markdown header
            md_file.write(f"# {output_filename}\n\n")
            
            # Write table header
            if rows:
                header = rows[0]
                md_file.write("| " + " | ".join(header) + " |\n")
                md_file.write("| " + " | ".join(["---"] * len(header)) + " |\n")
                
                # Write table rows
                for row in rows[1:]:
                    # Handle rows with varying lengths by padding with empty strings
                    padded_row = row + [''] * (len(header) - len(row))
                    md_file.write("| " + " | ".join(padded_row) + " |\n")

    print(f"Successfully converted {input_csv} to {output_path}")

if __name__ == "__main__":
    # Example usage
    csv_file = "data.csv"  # Replace with your actual CSV file path
    csv_to_markdown(csv_file)