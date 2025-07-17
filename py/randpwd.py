''' Generate a random password of a system user. '''
import random

s, l = [0, 0, 0], [0, 0, 0]
s[0] = 'qwertyuioplkjhgfdsazxcvbnmQWERTYUIOPLKJHGFDSAZXCVBNM'
s[1] = '1234567890'
s[2] = '~_+-./'
l[0], l[1], l[2] = random.randint(3, 6), random.randint(3, 5), random.randint(3, 4)
#print(s[2][l[0]], l[0])

pwd = []
for k in range(3):
    for i in range(l[k]):
        n = random.randint(0, len(s[k])-1)
        pwd.append(s[k][n])
random.shuffle(pwd)
print(''.join(pwd))