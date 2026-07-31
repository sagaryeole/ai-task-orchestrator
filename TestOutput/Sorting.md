# Sorting

Sorting is a fundamental operation in computer science that arranges elements of a list or array in a specific order (ascending or descending).

## Common Sorting Algorithms

### 1. Bubble Sort
Bubble sort repeatedly steps through the list, compares adjacent elements and swaps them if they are in the wrong order.

```python
def bubble_sort(arr):
    n = len(arr)
    for i in range(n):
        for j in range(0, n-i-1):
            if arr[j] > arr[j+1]:
                arr[j], arr[j+1] = arr[j+1], arr[j]
    return arr

# Example usage
data = [64, 34, 25, 12, 22, 11, 90]
sorted_data = bubble_sort(data.copy())
print("Bubble Sort:", sorted_data)
```

### 2. Selection Sort
Selection sort divides the input list into two parts: the sorted part at the beginning and the unsorted part at the end. It repeatedly finds the minimum element from the unsorted part and moves it to the end of the sorted part.

```python
def selection_sort(arr):
    n = len(arr)
    for i in range(n):
        min_idx = i
        for j in range(i+1, n):
            if arr[j] < arr[min_idx]:
                min_idx = j
        arr[i], arr[min_idx] = arr[min_idx], arr[i]
    return arr

# Example usage
data = [64, 34, 25, 12, 22, 11, 90]
sorted_data = selection_sort(data.copy())
print("Selection Sort:", sorted_data)
```

### 3. Insertion Sort
Insertion sort builds the final sorted array one item at a time. It is much less efficient on large lists than more advanced algorithms such as quicksort, heapsort, or merge sort.

```python
def insertion_sort(arr):
    for i in range(1, len(arr)):
        key = arr[i]
        j = i - 1
        while j >= 0 and key < arr[j]:
            arr[j + 1] = arr[j]
            j -= 1
        arr[j + 1] = key
    return arr

# Example usage
data = [64, 34, 25, 12, 22, 11, 90]
sorted_data = insertion_sort(data.copy())
print("Insertion Sort:", sorted_data)
```

### 4. Merge Sort
Merge sort is a divide-and-conquer algorithm. It divides the input list into two halves, calls itself for the two halves, and then merges the two sorted halves.

```python
def merge_sort(arr):
    if len(arr) <= 1:
        return arr
    
    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])
    
    return merge(left, right)

def merge(left, right):
    result = []
    i = j = 0
    
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1
    
    result.extend(left[i:])
    result.extend(right[j:])
    return result

# Example usage
data = [64, 34, 25, 12, 22, 11, 90]
sorted_data = merge_sort(data)
print("Merge Sort:", sorted_data)
```

### 5. Quick Sort
Quick sort is also a divide-and-conquer algorithm. It picks an element as a pivot and partitions the given array around the picked pivot.

```python
def quick_sort(arr):
    if len(arr) <= 1:
        return arr
    
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    
    return quick_sort(left) + middle + quick_sort(right)

# Example usage
data = [64, 34, 25, 12, 22, 11, 90]
sorted_data = quick_sort(data)
print("Quick Sort:", sorted_data)
```

## Built-in Sort
Python provides a built-in `sort()` method for lists and a `sorted()` function that returns a new sorted list.

```python
data = [64, 34, 25, 12, 22, 11, 90]
data.sort()  # Sorts in-place
print("In-place Sort:", data)

data = [64, 34, 25, 12, 22, 11, 90]
sorted_data = sorted(data)  # Returns a new list
print("New List Sort:", sorted_data)
```

## Complexity Analysis

| Algorithm      | Average Time | Worst Time | Space Complexity | Stability |
|----------------|--------------|------------|------------------|-----------|
| Bubble Sort    | O(n²)        | O(n²)      | O(1)             | Stable     |
| Selection Sort | O(n²)        | O(n²)      | O(1)             | Unstable   |
| Insertion Sort | O(n²)        | O(n²)      | O(1)             | Stable     |
| Merge Sort     | O(n log n)   | O(n log n) | O(n)             | Stable     |
| Quick Sort     | O(n log n)   | O(n²)      | O(log n)         | Unstable   |

## Conclusion
Sorting algorithms are essential for organizing data efficiently. While built-in methods like `sort()` and `sorted()` are optimized for general use, understanding the underlying algorithms helps in choosing the right approach for specific scenarios, such as when memory usage or stability is a concern.
