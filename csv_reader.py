import csv

def read_csv_file(filename):
    """
    Reads a CSV file and returns the data as a list of dictionaries.
    """
    data = []
    try:
        with open(filename, 'r', newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                data.append(row)
        return data
    except FileNotFoundError:
        print(f"Error: The file '{filename}' was not found.")
        return []
    except Exception as e:
        print(f"An error occurred: {e}")
        return []

def main():
    # Example usage: Replace 'example.csv' with your actual file name
    filename = 'example.csv'
    data = read_csv_file(filename)
    
    if data:
        print(f"Successfully read {len(data)} rows from {filename}:")
        for row in data:
            print(row)

if __name__ == "__main__":
    main()
