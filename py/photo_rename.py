''' Rename photos to a form like "2024-01-01 123001.xx" 
                  according to their date taken information. '''
import os
import exifread  # pip install "exifread<3"


def get_list(directory):
    l, files = [], os.listdir(directory)
    for file in files:
        #if file.startswith(("IMG_", "VID_")):
        if file.lower().endswith((".jpg", ".heic", ".mp4", ".mov")):
            l.append(os.path.join(directory, file))
    return l

def get_photo_time(file_path):
    with open(file_path, 'rb') as f:
        tags = exifread.process_file(f)
        try:
            datetime_original = tags['EXIF DateTimeOriginal'].printable
            return datetime_original
        except:
            return None

def batch_rename(directory, files):
    photo_time, no_time_photo = [], []
    for file in files:
        photo_time.append(get_photo_time(file))
        if photo_time[-1] is None:
            no_time_photo.append(file)
            continue
        tmp = photo_time[-1].split(" ")
        t0, t1 = tmp[0].split(":"), tmp[1].split(":")
        y, m, d, t = t0[0], t0[1], t0[2], t1[0]+t1[1]+t1[2]
        while True:
            file2 = y+'-'+m+'-'+d + ' '+t
            new_name = os.path.join(directory, file2 + "." + (file.split("."))[-1])
            try:
                print("%-*s -> %s" % (50, file, new_name))
                os.rename(file, new_name)
                break
            except:
                t = str(int(t) + 1)
    
    if len(no_time_photo):
        print("\n------ DateTime not exists: ------")
        for x in no_time_photo:
            print(x)

def batch_rename_Redmi13C(directory, prefix):  # Redmi 13C
    print(f"\n------ batch_rename_Redmi13C: {prefix} ------")
    files = os.listdir(directory)
    for file in files:
        if not file.startswith(prefix):
            continue
        old_name = os.path.join(directory, file)
        file1 = file.strip(prefix)
        y, m, d, t = file1[:4], file1[4:6], file1[6:8], file1[9:]
        while True:
            file2 = y+'-'+m+'-'+d + ' '+t
            new_name = os.path.join(directory, file2)
            try:
                print("%-*s -> %s" % (50, old_name, new_name))
                os.rename(old_name, new_name)
                break
            except:
                t = str(int(t) + 1)


if __name__ == "__main__":
    #directory = "test"
    directory = "202411GuGong"

    photo_list = get_list(directory)
    #print(photo_list)
    batch_rename(directory, photo_list)

    batch_rename_Redmi13C(directory, "VID_")